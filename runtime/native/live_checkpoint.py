"""Linux filesystem-consistent checkpoints with independent thaw guardians.

The caller holds the machine lifecycle lock. Nothing here takes that lock in
the detached watchdog: a dead worker must not prevent its own recovery.
Receipts remain pending whenever resume/thaw cannot be positively established.
Guest control is opened before freezing; new SSH authentication is not needed
to thaw the successful path. No guest/customer data is journaled or logged.
"""
import json
import os
import pathlib
import re
import select
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Unsupported hosts can still import capability metadata.
    fcntl = None


class CheckpointRecoveryPending(Exception):
    """Do not release storage admission or declare this attempt failed yet."""


class CheckpointAborted(ValueError):
    """Attempt invalidated, but the guest is positively released."""


def process_identity(pid):
    fields = pathlib.Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'boot_id': pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'start_ticks': fields[19], 'state': fields[0]}


def same_process(identity):
    return identity_status(identity) == 'same'


def identity_status(identity):
    try:
        current = process_identity(identity['pid'])
        if current['state'] == 'Z': return 'exited'
        return 'same' if all(current[k] == identity[k] for k in ('pid', 'boot_id', 'start_ticks')) else 'different'
    except FileNotFoundError as error:
        return 'exited' if error.filename == '/proc/%d/stat' % identity['pid'] else 'unknown'
    except (OSError, ValueError, KeyError):
        return 'unknown'


def signal_bound(identity, value):
    """A pidfd prevents a /proc check-to-signal race from targeting PID reuse."""
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'): return False
    try: fd = os.pidfd_open(identity['pid'], 0)
    except OSError: return False
    try:
        if not same_process(identity): return False
        signal.pidfd_send_signal(fd, value, None, 0)
        return True
    except OSError: return False
    finally: os.close(fd)


def save(path, item):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + '.new-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(item, stream, separators=(',', ':')); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def attempt_lock(path):
    if fcntl is None: raise ValueError('Checkpoint journal locks require Unix')
    fd = os.open(str(path)+'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


# Fixed program text is the only remote command argument. Control values go
# over private stdin. All state is on verified tmpfs, never on the frozen root.
GUEST_HELPER = r'''
import os,sys,json,time,signal,fcntl,stat,select,errno
signal.signal(signal.SIGHUP,signal.SIG_IGN)
config=json.loads(sys.stdin.readline())
nonce=config['nonce']
if len(nonce)!=32 or any(c not in '0123456789abcdef' for c in nonce):sys.exit(2)
mounts=[line.split() for line in open('/proc/self/mountinfo')]
def fs(path):
 for row in mounts:
  if row[4]==path:return row[row.index('-')+1]
if fs('/')!='ext4' or fs('/run')!='tmpfs':sys.exit(2)
base='/run/mola-checkpoints';os.makedirs(base,mode=0o700,exist_ok=True)
if os.path.islink(base) or os.stat(base).st_uid!=0 or stat.S_IMODE(os.stat(base).st_mode)!=0o700:sys.exit(2)
folder=base+'/'+nonce;os.mkdir(folder,0o700)
root=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
boot=open('/proc/sys/kernel/random/boot_id').read().strip()
parent=os.getpid();deadline=time.monotonic()+config['lease_seconds']
parent_ticks=open('/proc/'+str(parent)+'/stat').read().rsplit(')',1)[1].split()[19]
if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):sys.exit(2)
parent_pidfd=os.pidfd_open(parent,0)
def parent_identity():
 try:
  fields=open('/proc/'+str(parent)+'/stat').read().rsplit(')',1)[1].split()
  current_boot=open('/proc/sys/kernel/random/boot_id').read().strip()
  if current_boot!=boot or fields[19]!=parent_ticks:return 'different'
  return 'same' if fields[0]!='Z' else 'exited'
 except FileNotFoundError as e:
  return 'exited' if e.filename=='/proc/'+str(parent)+'/stat' else 'unknown'
 except Exception:return 'unknown'
def state(phase):
 p=folder+'/state';tmp=p+'.'+str(os.getpid())
 with open(tmp,'w') as f:json.dump({'nonce':nonce,'boot_id':boot,'phase':phase},f);f.flush();os.fsync(f.fileno())
 os.replace(tmp,p)
def released():
 # Root-filesystem fsync proves the freeze was released, not just that an
 # ioctl was attempted. A stuck I/O operation cannot produce positive proof.
 p='/var/tmp/.mola-checkpoint-'+nonce
 if os.stat('/var/tmp').st_dev!=os.fstat(root).st_dev:raise RuntimeError()
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 os.write(fd,b'released');os.fsync(fd);os.close(fd);os.unlink(p)
 state('released')
def thaw():
 try:fcntl.ioctl(root,0xC0045878,0)
 except OSError as e:
  if e.errno not in (errno.EINVAL,errno.EOPNOTSUPP):raise
def output(phase):
 print(json.dumps({'nonce':nonce,'boot_id':boot,'phase':phase}),flush=True)
state('ready')
child=os.fork()
if child==0:
 # Independent root guardian survives the SSH session. If the freezer was
 # stuck inside FIFREEZE, kill it and keep thawing until it has exited; a
 # late syscall completion can never qualify a snapshot or a release proof.
 os.setsid();null=os.open('/dev/null',os.O_RDWR)
 for fd in (0,1,2):os.dup2(null,fd)
 while True:
  try:
   if json.load(open(folder+'/state'))['phase']=='released':os._exit(0)
  except Exception:pass
  if time.monotonic()>=deadline:
   # Bind the original freezer's PID, boot and start ticks. A delayed guardian
   # must never signal an unrelated process after the old PID was recycled.
   identity=parent_identity()
   if identity=='same':
    try:signal.pidfd_send_signal(parent_pidfd,signal.SIGKILL,None,0)
    except ProcessLookupError:pass
   try:thaw()
   except Exception:pass
   if parent_identity() in ('exited','different'):
    try:thaw();released();os._exit(0)
    except Exception:pass
  time.sleep(.2)
output('ready')
try:
 for line in sys.stdin:
  command=json.loads(line)
  if command.get('nonce')!=nonce:raise RuntimeError()
  if command['command']=='freeze':
   if time.monotonic()>=deadline:raise RuntimeError()
   state('freezing');fcntl.ioctl(root,0xC0045877,0);state('frozen');output('frozen')
  elif command['command']=='thaw':
   thaw();released();output('released');break
  else:raise RuntimeError()
finally:
 try:thaw();released()
 except Exception:pass
'''

GUEST_READ = "import json,sys,re; n=sys.stdin.readline().strip(); assert re.fullmatch('[0-9a-f]{32}',n); print(open('/run/mola-checkpoints/'+n+'/state').read(1025))"


class GuestControl:
    def __init__(self, runner, data, journal):
        self.nonce = journal['nonce']
        self.process = subprocess.Popen(ssh_command(runner.config, data, GUEST_HELPER, persistent=True),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        self.process.stdin.write((json.dumps({'nonce': self.nonce, 'lease_seconds': journal['lease_seconds']}) + '\n').encode())
        self.process.stdin.flush()

    def receive(self, phase, timeout=15):
        deadline = time.monotonic() + timeout
        line = b''
        while time.monotonic() < deadline and len(line) <= 1024:
            if not select.select([self.process.stdout], [], [], max(0, deadline-time.monotonic()))[0]: break
            byte = os.read(self.process.stdout.fileno(), 1)
            if not byte: break
            line += byte
            if byte == b'\n':
                result = json.loads(line)
                if result.get('nonce') == self.nonce and result.get('phase') == phase and re.fullmatch('[0-9a-f-]{36}', result.get('boot_id', '')):
                    return result
                break
        raise CheckpointRecoveryPending('Checkpoint guest control is uncertain')

    def command(self, command, phase):
        self.process.stdin.write((json.dumps({'nonce': self.nonce, 'command': command})+'\n').encode()); self.process.stdin.flush()
        return self.receive(phase)

    def close(self):
        try:
            if self.process.stdin: self.process.stdin.close()
        except OSError: pass
        try: self.process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError): pass  # Independent guardian owns recovery.


def ssh_command(config, data, program, persistent=False):
    key, known = pathlib.Path(config['guest_key']), pathlib.Path(config['guest_known_hosts'])
    if not key.is_file() or key.is_symlink() or not known.is_file() or known.is_symlink():
        raise ValueError('Checkpoint SSH trust is not configured')
    if not isinstance(data.get('ssh_port'), int) or not 1 <= data['ssh_port'] <= 65535:
        raise ValueError('Invalid checkpoint SSH port')
    if not isinstance(data.get('id'), str) or not re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', data['id']):
        raise ValueError('Invalid checkpoint SSH identity')
    keepalive = ['-oServerAliveInterval=0', '-oTCPKeepAlive=no'] if persistent else ['-oServerAliveInterval=2', '-oServerAliveCountMax=3']
    return ['ssh', '-F', '/dev/null', '-oBatchMode=yes', '-oStrictHostKeyChecking=yes', '-oIdentitiesOnly=yes',
            '-oHostKeyAlias=mola-'+data['id'],
            '-oUserKnownHostsFile='+str(known), '-oConnectTimeout=5', *keepalive,
            '-p', str(data['ssh_port']), '-i', str(key), 'dev@127.0.0.1',
            'sudo -n python3 -u -c '+shlex.quote(program)]


def qmp(journal, command):
    connection = socket.socket(socket.AF_UNIX); connection.settimeout(3)
    try: connection.connect(journal['qmp_socket'])
    except BaseException:
        connection.close(); raise
    with connection, connection.makefile('rwb') as wire:
        if 'QMP' not in json.loads(wire.readline(65536)): raise ValueError('Invalid QMP peer')
        def execute(name):
            wire.write((json.dumps({'execute': name})+'\n').encode()); wire.flush()
            for _ in range(100):
                item = json.loads(wire.readline(65536))
                if 'return' in item: return item['return']
                if 'error' in item: raise ValueError('Checkpoint QMP rejected command')
            raise ValueError('Checkpoint QMP event bound exceeded')
        execute('qmp_capabilities')
        if execute('query-name').get('name') != 'mola-'+journal['computer_id']: raise ValueError('Checkpoint QMP identity mismatch')
        return execute(command)


def watchdog(path):
    """Detached process; never acquires or inherits the lifecycle lease."""
    path = pathlib.Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        return 2
    journal = json.loads(path.read_text())
    nonce = journal['nonce']
    if not re.fullmatch('[0-9a-f]{32}', nonce): return 2
    while True:
        current = json.loads(path.read_text())
        if current['nonce'] != nonce: return 2
        if current['phase'] in ('released', 'sealed', 'aborted'): return 0
        if time.monotonic() >= journal['monotonic_deadline'] or not same_process(journal['worker']): break
        time.sleep(.2)
    # Persist invalidation BEFORE resume. Even if a VM is later paused again,
    # no byte from this interrupted copy can be published as consistent.
    with attempt_lock(path):
        current = json.loads(path.read_text())
        if current['nonce'] != nonce or current['phase'] in ('released', 'sealed', 'aborted'): return 0
        try: save(path.with_name(path.name+'.abort'), {'nonce': nonce})
        except OSError: pass  # Safety recovery takes priority; deadline still fences copy.
    while identity_status(journal['qemu']) not in ('exited', 'different'):
        try:
            metadata = json.loads(pathlib.Path(journal['machine_path']).read_text())
            incarnation = metadata.get('storage_incarnation') or metadata.get('create_fingerprint') or 'legacy'
            if metadata.get('id') != journal['computer_id'] or metadata.get('managed_by') != 'mola-native-v1' or incarnation != journal['incarnation']: return 0
            if identity_status(journal['qemu']) != 'same': raise ValueError('Checkpoint process identity is uncertain')
            qmp(journal, 'cont')
            if qmp(journal, 'query-status').get('status') == 'running': break
        except (OSError, ValueError, KeyError): pass
        # Never abandon a paused live guest after an arbitrary retry count.
        # Its tmpfs guardian cannot advance until the virtual CPUs resume.
        time.sleep(.5)
    # Closing exactly the old SSH control process forces the already-open
    # helper's EOF recovery; never kill a reused PID or a different VM process.
    ssh = json.loads(path.read_text()).get('ssh')
    if ssh: signal_bound(ssh, signal.SIGTERM)
    return 0


class Services:
    def guest(self, runner, data, journal): return GuestControl(runner, data, journal)
    def identity(self, pid): return process_identity(pid)
    def alive(self, identity): return same_process(identity)
    def status(self, identity): return identity_status(identity)
    def start_watchdog(self, path):
        return subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), '--watchdog', str(path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, start_new_session=True)
    def read_released(self, runner, data, journal):
        result = subprocess.run(ssh_command(runner.config, data, GUEST_READ), input=(journal['nonce']+'\n').encode(),
                                capture_output=True, timeout=10, close_fds=True)
        if result.returncode != 0 or len(result.stdout) > 1024: return False
        response = json.loads(result.stdout)
        return response.get('nonce') == journal['nonce'] and response.get('phase') == 'released' and response.get('boot_id') == journal.get('guest_boot_id')


def checkpoint(runner, identifier, payload):
    """Run once or recover the original attempt; never silently refreeze."""
    if sys.platform != 'linux' and not hasattr(runner, '_checkpoint_services'):
        raise ValueError('Live checkpoint requires a qualified Linux host')
    operation = payload.get('operation_id')
    if not isinstance(operation, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', operation): raise ValueError('Invalid checkpoint operation')
    receipt = pathlib.Path(payload.get('_receipt_path', ''))
    if receipt != runner.operation_path(identifier, 'checkpoint', operation) or receipt.is_symlink():
        raise ValueError('Checkpoint requires its durable receipt')
    accepted = json.loads(receipt.read_text())
    if accepted.get('status') != 'pending' or accepted.get('incarnation') != runner.storage_incarnation(identifier):
        raise ValueError('Checkpoint receipt does not own this incarnation')
    public_payload = {key: value for key, value in payload.items() if key != '_receipt_path'}
    if accepted.get('payload') != public_payload:
        raise ValueError('Checkpoint receipt payload mismatch')
    services = getattr(runner, '_checkpoint_services', Services())
    data = runner.metadata(identifier)
    path = receipt.with_suffix('.checkpoint-state')  # Never mistaken for an operation receipt.
    abort = path.with_name(path.name+'.abort')
    if path.is_symlink() or abort.is_symlink(): raise ValueError('Unsafe checkpoint journal')
    guest = None
    if path.exists():
        journal = json.loads(path.read_text())
        if journal.get('operation_id') != operation or journal.get('incarnation') != accepted['incarnation']:
            raise ValueError('Checkpoint journal identity mismatch')
        return recover(runner, identifier, payload, services, data, path, journal)
    folder = runner.storage_path(identifier, payload['snapshot_id'])
    if (folder/'manifest.json').exists():
        manifest = runner.snapshot_manifest(identifier, payload['snapshot_id'])
        if manifest.get('checkpoint_operation_id') != operation: raise ValueError('Snapshot identity is already occupied')
        return manifest
    if runner.qmp(data, 'query-status').get('status') != 'running': raise ValueError('Checkpoint requires a running guest')
    lease = runner.config.get('checkpoint_lease_seconds', 120)
    if type(lease) is not int or not 10 <= lease <= 300: raise ValueError('Invalid checkpoint freeze bound')
    qemu = json.loads((runner.folder(identifier)/'running.marker').read_text())
    if not services.alive(qemu): raise ValueError('Checkpoint requires managed process identity')
    journal = {'computer_id': identifier, 'operation_id': operation, 'incarnation': accepted['incarnation'],
               'nonce': uuid.uuid4().hex, 'phase': 'preparing', 'deadline': time.time()+lease,
               'monotonic_deadline': time.monotonic()+lease,
               'lease_seconds': lease, 'worker': services.identity(os.getpid()), 'qemu': qemu,
               'machine_path': str(runner.folder(identifier)/'machine.json'),
               'qmp_socket': str(runner.sockets/(identifier+'.sock'))}
    save(path, journal)
    try:
        # Start the independent host recovery process before any freeze intent.
        services.start_watchdog(path)
        guest = services.guest(runner, data, journal)
        journal['ssh'] = services.identity(guest.process.pid)
        save(path, journal)
        ready = guest.receive('ready')
        journal['guest_boot_id'] = ready['boot_id']; journal['phase'] = 'freezing'; save(path, journal)
        frozen = guest.command('freeze', 'frozen')
        if frozen['boot_id'] != journal['guest_boot_id']: raise ValueError('Guest boot changed')
        journal['phase'] = 'pausing'; save(path, journal)
        runner.qmp(data, 'stop')
        if runner.qmp(data, 'query-status').get('status') != 'paused': raise ValueError('Guest pause was not established')
        journal['phase'] = 'copying'; save(path, journal)
        last_probe = [time.monotonic()]
        def copying_guard():
            assert_attempt(path, journal, abort)
            if time.monotonic()-last_probe[0] >= 1:
                if runner.qmp(data, 'query-status').get('status') != 'paused': raise ValueError('Checkpoint guest resumed during copy')
                last_probe[0] = time.monotonic()
        manifest = runner._snapshot_contents(identifier, payload, guard=copying_guard, publish=False)
        copying_guard()
        if runner.qmp(data, 'query-status').get('status') != 'paused': raise ValueError('Checkpoint guest resumed during copy')
        manifest.update(checkpoint_operation_id=operation, checkpoint_attempt=str(uuid.UUID(journal['nonce'])), consistency='filesystem')
        journal['manifest'] = manifest; journal['phase'] = 'resuming'; save(path, journal)
        runner.qmp(data, 'cont')
        if runner.qmp(data, 'query-status').get('status') != 'running': raise CheckpointRecoveryPending('Checkpoint resume is uncertain')
        released = guest.command('thaw', 'released')
        if released['boot_id'] != journal['guest_boot_id']: raise CheckpointRecoveryPending('Checkpoint guest boot changed')
        # Guard before marking released, so an abort/deadline cannot become a
        # successful snapshot merely because thaw finally completed.
        with attempt_lock(path):
            assert_attempt(path, journal, abort)
            journal['manifest'].update(running_resumed=True, filesystem_thawed=True)
            journal['phase'] = 'released'; save(path, journal)
        return seal(runner, identifier, payload, path, journal, abort)
    except BaseException:
        # Positive recovery must precede a terminal failure. A worker kill is
        # handled independently; normal errors also try the existing channel.
        try: save(abort, {'nonce': journal['nonce']})
        except OSError: pass
        if guest:
            try:
                runner.qmp(data, 'cont')
                if runner.qmp(data, 'query-status').get('status') == 'running':
                    released = guest.command('thaw', 'released')
                    if released['boot_id'] == journal.get('guest_boot_id'):
                        journal['phase'] = 'aborted'; save(path, journal)
                        raise CheckpointAborted('Checkpoint attempt was safely aborted')
            except CheckpointAborted: raise
            except Exception: pass
        raise CheckpointRecoveryPending('Checkpoint resume or thaw requires reconciliation') from None
    finally:
        if guest:
            try: guest.close()
            except Exception: pass  # Never turn uncertain recovery into terminal failure.


def assert_attempt(path, journal, abort):
    if abort.exists() or time.monotonic() >= journal['monotonic_deadline']:
        raise ValueError('Checkpoint attempt was invalidated')
    if json.loads(path.read_text()).get('nonce') != journal['nonce']:
        raise ValueError('Checkpoint attempt changed')


def seal(runner, identifier, payload, path, journal, abort):
    def guard():
        # Once released the pause lease no longer applies, but a persisted
        # guardian abort or different attempt always invalidates this copy.
        current = json.loads(path.read_text())
        if abort.exists() or current.get('nonce') != journal['nonce'] or current.get('phase') not in ('released', 'sealed'):
            raise ValueError('Checkpoint publication was invalidated')
    with attempt_lock(path):
        result = runner._publish_snapshot(identifier, payload, journal['manifest'], guard=guard)
        if result.get('checkpoint_operation_id') != journal['operation_id'] or result.get('checkpoint_attempt') != str(uuid.UUID(journal['nonce'])):
            raise ValueError('Checkpoint artifact identity mismatch')
        journal['phase'] = 'sealed'; save(path, journal)
    return result


def recover(runner, identifier, payload, services, data, path, journal):
    abort = path.with_name(path.name+'.abort')
    if journal['phase'] in ('released', 'sealed') and not abort.exists():
        return seal(runner, identifier, payload, path, journal, abort)
    if journal['phase'] == 'aborted': raise CheckpointAborted('Checkpoint attempt was safely aborted')
    try: save(abort, {'nonce': journal['nonce']})
    except OSError: raise CheckpointRecoveryPending('Checkpoint recovery journal is unavailable') from None
    if journal['phase'] == 'preparing':
        # No freeze command can precede the durable freezing phase. An idle
        # pre-opened helper only owns a root fd and its independent guardian.
        ssh = journal.get('ssh')
        if ssh and services.alive(ssh): signal_bound(ssh, signal.SIGTERM)
        journal['phase'] = 'aborted'; save(path, journal)
        raise CheckpointAborted('Checkpoint stopped before freeze submission')
    # Exited original QEMU implies the frozen in-memory kernel is gone; never
    # issue cont to an unrelated or reused process. No old copy is published.
    status = services.status(journal['qemu']) if hasattr(services, 'status') else ('same' if services.alive(journal['qemu']) else 'exited')
    if status in ('exited', 'different'):
        journal['phase'] = 'aborted'; save(path, journal)
        raise CheckpointAborted('Checkpoint original guest has exited')
    if status != 'same': raise CheckpointRecoveryPending('Checkpoint process exit is not established')
    try:
        runner.qmp(data, 'cont')
        if runner.qmp(data, 'query-status').get('status') != 'running': raise ValueError()
        ssh = journal.get('ssh')
        if ssh and services.alive(ssh): signal_bound(ssh, signal.SIGTERM)
        if not services.read_released(runner, data, journal): raise ValueError()
    except Exception:
        raise CheckpointRecoveryPending('Checkpoint thaw has not been established') from None
    journal['phase'] = 'aborted'; save(path, journal)
    raise CheckpointAborted('Interrupted checkpoint was safely released')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--watchdog':
        try: sys.exit(watchdog(sys.argv[2]))
        except Exception: sys.exit(1)  # Never print private configuration/errors.
    sys.exit(2)
