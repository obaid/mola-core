"""Authenticated, local QEMU supervisor. The control plane owns policy and billing.

Only an operator-selected image/runtime may run. Request data never selects a
host path, command, display backend, or QEMU argument. Guest ports bind loopback.
"""
import argparse
import base64
import gzip
import hashlib
import hmac
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import threading
import time

_checkpoint_spec = importlib.util.spec_from_file_location('mola_live_checkpoint', Path(__file__).with_name('live_checkpoint.py'))
_checkpoint_module = importlib.util.module_from_spec(_checkpoint_spec)
_checkpoint_spec.loader.exec_module(_checkpoint_module)

OPERATION_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}')
DURABLE_STORAGE_VERBS = ('snapshot', 'checkpoint', 'restore', 'snapshot-delete', 'snapshot-import', 'snapshot-seal', 'fence', 'resize')


class StorageConflict(ValueError):
    pass


ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SNAPSHOT_CHUNK_BYTES = 8 * 1024 * 1024
SNAPSHOT_CHUNK_ENCODED_BYTES = 11184812
DEFAULT_REQUEST_BYTES = 1536 * 1024
# PHP escapes `/` in base64 JSON as `\/`; reserve for the worst-case wire
# representation, then enforce the decoded 8 MiB limit in write_snapshot_chunk.
SNAPSHOT_WRITE_REQUEST_BYTES = (2 * SNAPSHOT_CHUNK_ENCODED_BYTES) + 64 * 1024


def request_body_limit(path, method):
    """Keep ordinary loopback calls small while admitting one snapshot chunk."""
    parts = path.strip('/').split('/')
    if method == 'POST' and len(parts) == 3 and parts[0] == 'machines' and parts[2] == 'snapshot-write':
        return SNAPSHOT_WRITE_REQUEST_BYTES
    return DEFAULT_REQUEST_BYTES


def write_json(path, value):
    temporary = path.with_suffix('.new')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def seed_disk(path, text):
    """A FAT16 superfloppy readable by Linux; no host mkfs dependency."""
    data = text.encode('utf-8')
    if len(data) > 65536: raise ValueError('Identity payload is too large')
    sectors, fat_sectors, root_sectors, cluster_bytes = 32768, 32, 32, 2048
    boot = bytearray(512)
    boot[:11] = b'\xeb\x3c\x90MOLA    '
    struct.pack_into('<HBHBHHBHHHII', boot, 11, 512, 4, 1, 2, 512, sectors, 0xf8, fat_sectors, 32, 64, 0, 0)
    boot[36:39] = b'\x80\x00\x29'
    boot[43:54] = b'MOLA       '
    boot[54:62] = b'FAT16   '
    boot[510:] = b'\x55\xaa'
    fat = bytearray(fat_sectors * 512)
    struct.pack_into('<HH', fat, 0, 0xfff8, 0xffff)
    count = max(1, (len(data) + cluster_bytes - 1) // cluster_bytes)
    for index in range(count):
        struct.pack_into('<H', fat, (index + 2) * 2, 0xffff if index == count - 1 else index + 3)
    entry = bytearray(root_sectors * 512)
    entry[:11] = b'IDENTITYENV'
    entry[11] = 0x20
    struct.pack_into('<H', entry, 26, 2)
    struct.pack_into('<I', entry, 28, len(data))
    with path.open('wb') as disk:
        disk.write(boot); disk.write(fat); disk.write(fat); disk.write(entry); disk.write(data)
        disk.truncate(sectors * 512)
    os.chmod(path, 0o600)


def accelerator(system, host_arch, guest_arch):
    normalized = {'arm64': 'aarch64', 'AMD64': 'x86_64', 'amd64': 'x86_64'}.get(host_arch, host_arch)
    if normalized != guest_arch:
        raise ValueError('Guest and host CPU architecture must match; this runner does not silently emulate CPUs')
    if system == 'Darwin' and guest_arch == 'aarch64': return 'hvf'
    if system == 'Linux': return 'kvm'
    if system == 'Windows' and guest_arch == 'x86_64': return 'whpx'
    raise ValueError('Unsupported native host/guest combination')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Runner:
    def __init__(self, config_path):
        self.config_path = config_path.resolve()
        self.root = self.config_path.parent
        self.config = json.loads(config_path.read_text())
        self.arch = self.config['architecture']
        self.accel = accelerator(platform.system(), platform.machine(), self.arch)
        self.qemu = Path(self.config['qemu']).resolve()
        self.image = Path(self.config['image']).resolve()
        self.default_image_ref = self.config.get('default_image_ref', 'omarchy-agent:0.1.0')
        self.images = {self.default_image_ref: {
            'path': str(self.image), 'architecture': self.arch,
            'kernel_args': self.config['kernel_args'], 'gpu': self.config['gpu'], 'display': self.config['display'],
        }, **self.config.get('images', {})}
        for image in self.images.values():
            if image.get('architecture') != self.arch:
                raise ValueError('Installed image architecture does not match this host')
            directory = Path(image['path']).resolve()
            if not Path(image['path']).is_absolute(): raise ValueError('Image path must be absolute')
            if any((directory / marker).exists() for marker in ['STAGING_INCOMPLETE', 'STAGING_FAILED']):
                raise ValueError('Refusing an incomplete or failed prepared image')
            for path in [self.qemu, directory, self.root]:
                if ',' in str(path) or '\n' in str(path): raise ValueError('QEMU paths cannot contain commas or newlines')
            for name in ['root.ext4', 'vmlinuz-linux', 'initramfs-linux.img']:
                if not (directory / name).is_file(): raise ValueError('Missing prepared image artifact: ' + name)
        if self.accel == 'kvm' and not os.access('/dev/kvm', os.R_OK | os.W_OK):
            raise ValueError('Linux requires usable /dev/kvm')
        capabilities = subprocess.check_output([str(self.qemu), '-accel', 'help'], text=True)
        if self.accel not in capabilities.split(): raise ValueError('QEMU lacks ' + self.accel)
        self.machines = self.root / 'machines'
        self.machines.mkdir(parents=True, exist_ok=True, mode=0o700)
        # systemd PrivateTmp creates a new /tmp namespace on service restart.
        # Linux QEMU processes survive that restart, so their QMP sockets must
        # live alongside persistent runtime state. macOS needs the short path.
        self.sockets = self.socket_directory(self.root, platform.system())
        if platform.system() != 'Windows' and len(os.fsencode(str(self.sockets / ('0' * 36 + '.sock')))) >= 104:
            raise ValueError('Runtime path is too long for a durable QMP socket; use a shorter MOLA_HOME')
        self.sockets.mkdir(mode=0o700, exist_ok=True)
        if self.sockets.is_symlink() or (platform.system() != 'Windows' and self.sockets.stat().st_uid != os.getuid()):
            raise ValueError('Unsafe socket directory')
        os.chmod(self.sockets, 0o700)
        self.lock = threading.RLock()
        self.machine_locks = {}
        self.machine_locks_guard = threading.Lock()
        self.processes = {}
        self.storage_guard = threading.RLock()
        self.storage_workers = {}

    def selected_image(self, data):
        ref = data.get('image_ref', getattr(self, 'default_image_ref', 'omarchy-agent:0.1.0'))
        # Old persisted machines predate the catalog and remain on the default.
        if not hasattr(self, 'images'):
            return {'path': str(self.image), 'kernel_args': self.config.get('kernel_args'),
                    'gpu': self.config.get('gpu'), 'display': self.config.get('display')}
        if ref not in self.images: raise ValueError('Machine image is not installed on this host')
        return self.images[ref]

    @staticmethod
    def socket_directory(root, system):
        if system == 'Linux': return root / 'sockets'
        if system == 'Windows': return root
        return Path('/tmp') / ('mola-' + str(os.getuid()) + '-' + hashlib.sha256(str(root).encode()).hexdigest()[:12])

    @staticmethod
    def linux_process_identity(pid):
        # Parse after the final ')' because the comm field may contain spaces
        # and parentheses. starttime plus the boot UUID protects against PID reuse.
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'boot_id': boot_id, 'start_ticks': fields[19], 'state': fields[0]}

    def clear_running_marker(self, identifier):
        (self.folder(identifier) / 'running.marker').unlink(missing_ok=True)
        if platform.system() != 'Windows': (self.sockets / (identifier + '.sock')).unlink(missing_ok=True)
        self.sync_directory(self.folder(identifier))

    def known_process_exited(self, marker):
        if platform.system() != 'Linux': return False
        try:
            evidence = json.loads(marker.read_text())
            if not isinstance(evidence.get('pid'), int) or not evidence.get('boot_id') or not evidence.get('start_ticks'): return False
            current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if current_boot != evidence['boot_id']: return True
            try: current = self.linux_process_identity(evidence['pid'])
            except FileNotFoundError: return True
            return current['start_ticks'] != evidence['start_ticks'] or current['state'] in ('Z', 'X')
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            # An old empty marker, incomplete launch receipt, permissions failure
            # or malformed /proc observation supplies no stopped evidence.
            return False

    def machine_lock(self, identifier):
        self.folder(identifier)  # Validate IDs before allocating lock entries.
        # Test fixtures may construct a runner without launching QEMU setup.
        if not hasattr(self, 'machine_locks'):
            self.machine_locks, self.machine_locks_guard = {}, threading.Lock()
        with self.machine_locks_guard:
            return self.machine_locks.setdefault(identifier, threading.RLock())

    def folder(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier): raise ValueError('Invalid computer id')
        folder = self.machines / identifier
        if folder.is_symlink(): raise ValueError('Machine directory must not be a symlink')
        return folder

    def metadata(self, identifier):
        data = json.loads((self.folder(identifier) / 'machine.json').read_text())
        if data.get('id') != identifier or data.get('managed_by') != 'mola-native-v1':
            raise ValueError('Refusing unmanaged machine')
        return data

    def qmp(self, data, command):
        if platform.system() == 'Windows':
            connection = socket.create_connection(('127.0.0.1', data['qmp_port']), timeout=3)
        else:
            connection = socket.socket(socket.AF_UNIX)
            connection.settimeout(3)
            connection.connect(str(self.sockets / (data['id'] + '.sock')))
        with connection, connection.makefile('rwb') as wire:
            def response():
                for _ in range(100):
                    line = wire.readline(65536)
                    if not line: raise OSError('QMP closed the connection')
                    item = json.loads(line)
                    if 'return' in item: return item['return']
                    if 'error' in item: raise ValueError('QMP rejected command: ' + str(item['error']))
                raise OSError('Too many QMP events')
            if 'QMP' not in json.loads(wire.readline(65536)): raise OSError('Not a QMP socket')
            wire.write(b'{"execute":"qmp_capabilities"}\n'); wire.flush(); response()
            wire.write(b'{"execute":"query-name"}\n'); wire.flush()
            if response().get('name') != 'mola-' + data['id']:
                raise ValueError('QMP machine identity mismatch')
            wire.write(json.dumps({'execute': command}).encode() + b'\n'); wire.flush()
            return response()

    def status(self, data):
        process = self.processes.get(data['id'])
        if process is not None and process.poll() is not None:
            self.processes.pop(data['id'], None)
            self.clear_running_marker(data['id'])
            return 'stopped'
        try:
            state = self.qmp(data, 'query-status')['status']
            return 'running' if state == 'running' else 'unknown'
        except (OSError, KeyError):
            # A live child without its management socket is never declared stopped.
            if process is not None: return 'starting'
            marker = self.folder(data['id']) / 'running.marker'
            if marker.exists():
                if self.known_process_exited(marker):
                    self.clear_running_marker(data['id'])
                    return 'stopped'
                return 'unknown'
            if platform.system() != 'Windows' and (self.sockets / (data['id'] + '.sock')).exists(): return 'unknown'
            return 'stopped'

    def describe(self, identifier):
        lock = self.machine_lock(identifier)
        if not lock.acquire(blocking=False):
            # A long disk operation on this machine is uncertain. Do not wait
            # behind it or expose a stale stopped observation as release proof.
            return {'provider_vm_id': identifier, 'status': 'unknown', 'disk_id': identifier,
                    'meta': {'runtime': self.accel, 'architecture': self.arch}}
        try:
            data = self.metadata(identifier)
            status = self.status(data)
            return {'provider_vm_id': identifier, 'status': status, 'disk_id': identifier,
                    'display_host': self.config.get('connect_host', 'host.docker.internal'),
                    'display_port': data['vnc_port'], 'ssh_host': self.config.get('connect_host', 'host.docker.internal'),
                    'ssh_port': data['ssh_port'], 'meta': {'runtime': self.accel, 'architecture': self.arch}}
        finally: lock.release()

    def create(self, spec):
        identifier = spec['computer_id']
        folder = self.folder(identifier)
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        if (folder / 'machine.json').exists():
            existing = self.metadata(identifier)
            if existing.get('create_fingerprint') and existing['create_fingerprint'] != fingerprint:
                raise ValueError('Machine ID already holds a different create specification')
            return self.describe(identifier)
        if folder.exists(): raise ValueError('Incomplete machine directory; inspect it before retrying')
        image = self.selected_image(spec)
        image_path = Path(image['path'])
        cpus, memory, disk_gb = spec['vcpus'], spec['memory_mb'], spec['disk_gb']
        if not all(type(n) is int for n in [cpus, memory, disk_gb]): raise ValueError('Resources must be integers')
        if not (1 <= cpus <= (8 if self.arch == 'aarch64' else 32) and 1024 <= memory <= self.config.get('max_memory_mb', 8192) and 16 <= disk_gb <= 1024):
            raise ValueError('Requested resources exceed native runner limits')
        if (image_path / 'root.ext4').stat().st_size > disk_gb * 1024**3:
            raise ValueError('Requested disk is smaller than the installed image')
        keys = spec.get('authorized_keys', [])
        if not isinstance(keys, list) or any(not isinstance(key, str) or len(key) > 16384 for key in keys):
            raise ValueError('Invalid authorized keys')
        fields = {'MOLA_ENDPOINT': json.loads(self.config_path.read_text())['guest_endpoint'],
                  'MOLA_REGISTRATION_TOKEN': spec['registration_token'],
                  'MOLA_COMPUTER_ID': identifier, 'MOLA_MACHINE_NAME': spec['name'],
                  'MOLA_AUTHORIZED_KEYS': '\n'.join(keys)}
        display = spec.get('display')
        if display is not None:
            if not spec.get('image_ref', '').startswith('ubuntu-xfce:') or not isinstance(display, dict) or set(display) != {'width','height'} or not (type(display['width']) is int and type(display['height']) is int and 640 <= display['width'] <= 3840 and 480 <= display['height'] <= 2160):
                raise ValueError('Invalid or unsupported display configuration')
            fields['MOLA_DISPLAY_GEOMETRY'] = str(display['width']) + 'x' + str(display['height'])
        if any(not isinstance(value, str) or '\x00' in value for value in fields.values()):
            raise ValueError('Invalid identity')
        # Guest images cached before the rename read HYPERWAKE_*, and an image
        # already on disk is never re-downloaded, so upgrading the engine alone
        # would leave those machines unable to find their control plane.
        # Writing both names costs a few hundred bytes on a 64 KB budget; the
        # guest prefers MOLA_. Removable once no such image is in circulation.
        fields.update({'HYPERWAKE_' + key.removeprefix('MOLA_'): value for key, value in fields.items()})
        payload = ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in fields.items())
        if len(payload.encode()) > 65536: raise ValueError('Identity payload is too large')
        # Preserved disks live outside active machine metadata and are never overwritten.
        retained = self.root / 'retained' / (identifier + '.ext4')
        if retained.exists():
            raise ValueError('A retained disk exists; explicit recovery is required')
        destination = folder
        folder = self.machines / ('.creating-' + identifier)
        # Only an unpublished clone with exactly this intent may be discarded.
        # A crash during copy therefore retries the same ID without touching a
        # live machine, a retained disk, or an unrelated directory.
        if folder.is_symlink(): raise ValueError('Unsafe staging directory')
        if folder.exists():
            marker = folder / 'intent.json'
            if marker.exists():
                if json.loads(marker.read_text()).get('fingerprint') != fingerprint:
                    raise ValueError('Staging directory belongs to another create intent')
            elif any(folder.iterdir()):
                raise ValueError('Unrecognized staging directory')
            shutil.rmtree(folder)
        folder.mkdir(mode=0o700)
        write_json(folder / 'intent.json', {'fingerprint': fingerprint})
        disk = folder / 'root.ext4'
        if platform.system() == 'Darwin':
            subprocess.run(['cp', '-c', str(image_path / 'root.ext4'), str(disk)], check=True)
        elif platform.system() == 'Linux':
            # Reflink where supported; otherwise retain a sparse independent copy.
            subprocess.run(['cp', '--reflink=auto', '--sparse=always', str(image_path / 'root.ext4'), str(disk)], check=True)
        else:
            # Copy in sparse blocks to avoid allocating the image's free space.
            if platform.system() == 'Windows':
                disk.touch()
                subprocess.run(['fsutil', 'sparse', 'setflag', str(disk)], check=True, stdout=subprocess.DEVNULL)
            with (image_path / 'root.ext4').open('rb') as source, disk.open('wb') as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    if chunk.strip(b'\0'): target.write(chunk)
                    else: target.seek(len(chunk), 1)
                target.truncate()
        with disk.open('r+b') as stream: stream.truncate(max(disk.stat().st_size, disk_gb * 1024**3))
        if spec.get('browser_proxy') is not None:
            if not spec.get('image_ref', '').startswith('ubuntu-xfce:'): raise ValueError('Browser proxy requires Ubuntu')
            from install_browser_proxy import install
            install(disk, spec['browser_proxy'])
        seed_disk(folder / 'identity.img', payload)
        ports = set()
        while len(ports) < 3: ports.add(free_port())
        ssh_port, vnc_port, qmp_port = sorted(ports)
        data = {'managed_by': 'mola-native-v1', 'id': identifier, 'vcpus': cpus, 'memory_mb': memory,
                'ssh_port': ssh_port, 'vnc_port': vnc_port, 'qmp_port': qmp_port, 'create_fingerprint': fingerprint,
                'storage_incarnation': secrets.token_hex(16), 'storage_generation': spec.get('incarnation_generation', 0),
                'image_ref': spec.get('image_ref', getattr(self, 'default_image_ref', 'omarchy-agent:0.1.0'))}
        if display is not None: data['display'] = display
        write_json(folder / 'machine.json', data)
        folder.rename(destination)
        return self.describe(identifier)

    def command(self, data):
        image = self.selected_image(data)
        image_path = Path(image['path'])
        folder = self.folder(data['id'])
        if self.arch == 'aarch64':
            # GICv2 is what the packaged runtime supports; stock QEMU on HVF
            # requires GICv3. Configurable so either can drive the same image.
            machine = 'virt,accel=' + self.accel + ',gic-version=' + str(self.config.get('gic_version', 2))
            cpu = 'host,pmu=off'
        else:
            machine = 'q35,accel=' + self.accel
            cpu = self.config.get('cpu', 'host')
        qmp = ('tcp:127.0.0.1:' + str(data['qmp_port'])) if platform.system() == 'Windows' else 'unix:' + str(self.sockets / (data['id'] + '.sock'))
        args = [str(self.qemu), '-name', 'mola-' + data['id'], '-machine', machine, '-cpu', cpu,
                '-smp', str(data['vcpus']), '-m', str(data['memory_mb']), '-nodefaults',
                '-kernel', str(image_path / 'vmlinuz-linux'), '-initrd', str(image_path / 'initramfs-linux.img'),
                '-append', image['kernel_args'], '-qmp', qmp + ',server=on,wait=off',
                '-drive', f'file={folder / "root.ext4"},if=none,id=root,format=raw', '-device', 'virtio-blk-pci,drive=root',
                '-drive', f'file={folder / "identity.img"},if=none,id=identity,format=raw,readonly=on', '-device', 'virtio-blk-pci,drive=identity',
                '-netdev', f'user,id=net,hostfwd=tcp:127.0.0.1:{data["ssh_port"]}-:22,hostfwd=tcp:127.0.0.1:{data["vnc_port"]}-:5900',
                '-device', 'virtio-net-pci,netdev=net,romfile=', '-device', image['gpu'], '-display', image['display'],
                '-device', 'virtio-keyboard-pci', '-device', 'virtio-tablet-pci', '-device', 'virtio-serial-pci',
                '-chardev', f'file,id=console,path={folder / "console.log"}', '-device', 'virtconsole,chardev=console',
                '-serial', 'none', '-monitor', 'none']
        return args

    def start(self, identifier):
        data = self.metadata(identifier)
        if data.get('fenced'): raise ValueError('Machine is fenced and cannot start on this host')
        current = self.status(data)
        if current in ('running', 'starting'): return self.describe(identifier)
        if current != 'stopped': raise ValueError('Machine state is uncertain; inspect it before starting')
        states = self.list()
        running = sum(value in ('running', 'starting', 'unknown') for value in states.values())
        memory = sum(self.metadata(key)['memory_mb'] for key, value in states.items() if value != 'stopped')
        if memory + data['memory_mb'] > self.config.get('max_memory_mb', 8192):
            raise ValueError('Native host memory limit reached')
        if running >= self.config.get('max_running', 2): raise ValueError('Native host running-computer limit reached')
        folder = self.folder(identifier)
        (self.sockets / (identifier + '.sock')).unlink(missing_ok=True)
        log = (folder / 'runtime.log').open('ab')
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if platform.system() == 'Windows' else {'start_new_session': True}
        try:
            # Save intent before Popen: a crash during launch is uncertain,
            # never evidence that the guest stopped.
            write_json(folder / 'running.marker', {'launching': True})
            self.sync_directory(folder)
            try:
                process = subprocess.Popen(self.command(data), stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
            except Exception:
                # Popen returned an error, so no child was successfully created.
                self.clear_running_marker(identifier)
                raise
            self.processes[identifier] = process
            if platform.system() == 'Linux':
                try:
                    identity = self.linux_process_identity(process.pid)
                    write_json(folder / 'running.marker', identity)
                    self.sync_directory(folder)
                except (OSError, ValueError, IndexError):
                    # The live Popen object can still prove exit; if this process
                    # crashes first, retain the conservative launch marker.
                    pass
        finally: log.close()
        time.sleep(0.3)
        if self.processes[identifier].poll() is not None:
            raise ValueError('QEMU exited during launch; inspect the machine runtime.log')
        return self.describe(identifier)

    def stop(self, identifier, force=False):
        data = self.metadata(identifier)
        if self.status(data) == 'stopped': return
        self.qmp(data, 'quit' if force else 'system_powerdown')

    def destroy(self, identifier, delete_disk):
        folder = self.folder(identifier)
        retained = self.root / 'retained' / (identifier + '.ext4')
        snapshots = self.root / 'snapshots' / identifier
        if not folder.exists():
            if delete_disk:
                retained.unlink(missing_ok=True)
                if snapshots.exists(): shutil.rmtree(snapshots)
            return
        data = self.metadata(identifier)
        if self.status(data) != 'stopped': raise ValueError('Stop the computer before deleting it')
        if not delete_disk:
            retained.parent.mkdir(exist_ok=True, mode=0o700)
            if retained.exists():
                if not data.get('retaining_disk') or (folder / 'root.ext4').exists():
                    raise ValueError('Retained disk already exists')
            else:
                data['retaining_disk'] = True
                write_json(folder / 'machine.json', data)
                (folder / 'root.ext4').rename(retained)
        shutil.rmtree(folder)
        if delete_disk and snapshots.exists(): shutil.rmtree(snapshots)

    def reseed(self, identifier, payload):
        self.require_stopped(identifier)
        if self.metadata(identifier).get('image_ref', '').startswith('ubuntu-xfce:') and 'browser_proxy' in payload:
            from install_browser_proxy import install
            install(self.folder(identifier) / 'root.ext4', payload.get('browser_proxy'), payload.get('network_binding'))
        fields = {'MOLA_ENDPOINT': json.loads(self.config_path.read_text())['guest_endpoint'],
                  'MOLA_REGISTRATION_TOKEN': payload['registration_token'],
                  'MOLA_COMPUTER_ID': identifier, 'MOLA_MACHINE_NAME': payload['name'],
                  'MOLA_AUTHORIZED_KEYS': '\n'.join(payload['authorized_keys'])}
        display = payload.get('display', self.metadata(identifier).get('display'))
        if display is not None:
            if not isinstance(display, dict) or set(display) != {'width', 'height'} or not (type(display['width']) is int and type(display['height']) is int and 640 <= display['width'] <= 3840 and 480 <= display['height'] <= 2160): raise ValueError('Invalid display configuration')
            fields['MOLA_DISPLAY_GEOMETRY'] = str(display['width']) + 'x' + str(display['height'])
        if any(not isinstance(value, str) or '\x00' in value for value in fields.values()): raise ValueError('Invalid identity')
        fields.update({'HYPERWAKE_' + key.removeprefix('MOLA_'): value for key, value in fields.items()})
        text = ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in fields.items())
        temporary = self.folder(identifier) / 'identity.new'
        seed_disk(temporary, text)
        with temporary.open('rb') as stream: os.fsync(stream.fileno())
        temporary.replace(self.folder(identifier) / 'identity.img')
        self.sync_directory(self.folder(identifier))
        if display is not None:
            data = self.metadata(identifier); data['display'] = display; write_json(self.folder(identifier) / 'machine.json', data); self.sync_directory(self.folder(identifier))
        return {'id': identifier, 'reseeded': True}

    def storage_path(self, identifier, snapshot_id):
        self.metadata(identifier)
        if not isinstance(snapshot_id, str) or not ID.fullmatch(snapshot_id):
            raise ValueError('Invalid snapshot id')
        folder = self.root / 'snapshots' / identifier / snapshot_id
        if any(path.is_symlink() for path in [folder, folder.parent, folder.parent.parent]):
            raise ValueError('Unsafe snapshot directory')
        return folder

    def create_storage_directory(self, folder):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir(parents=True) can create snapshots/, machine/ and snapshot/.
        # Fsync all containing directories before any receipt can acknowledge
        # an artifact under this namespace, including the root link itself.
        for parent in (folder.parent, folder.parent.parent, self.root):
            self.sync_directory(parent)

    def require_stopped(self, identifier):
        if self.status(self.metadata(identifier)) != 'stopped':
            raise ValueError('Stop the computer before changing its disk')

    def capabilities(self):
        return {'resize': True, 'resize_disk_grow': platform.system() == 'Linux' and all(shutil.which(t) for t in ['e2fsck', 'resize2fs', 'cp']),
                'resize_disk_shrink': False, 'resize_requires_stopped': True,
                'running_checkpoint': platform.system() == 'Linux' and self.config.get('running_checkpoint') is True,
                'running_checkpoint_reason': None if platform.system() == 'Linux' and self.config.get('running_checkpoint') is True else 'operator qualification gate is disabled',
                'resource_limits': {'vcpus': 8, 'memory_mb': min(16384, self.config.get('max_memory_mb', 8192)), 'disk_gb': 1024}}

    def checkpoint(self, identifier, payload):
        # Recovery remains available after an operator disables new admission.
        path = payload.get('_receipt_path')
        recovering = path and path.with_suffix('.checkpoint-state').exists()
        if not recovering and not self.capabilities()['running_checkpoint']:
            raise ValueError('Running checkpoint qualification is disabled')
        return _checkpoint_module.checkpoint(self, identifier, payload)

    def resize(self, identifier, payload):
        self.require_stopped(identifier)
        data = self.metadata(identifier)
        cpus, memory, disk_gb = (payload.get(k) for k in ['vcpus', 'memory_mb', 'disk_gb'])
        if not all(type(v) is int for v in [cpus, memory, disk_gb]) or not (1 <= cpus <= 8 and 1024 <= memory <= min(16384, self.config.get('max_memory_mb', 8192)) and 16 <= disk_gb <= 1024):
            raise ValueError('Invalid resize resources')
        destination = self.folder(identifier) / 'root.ext4'
        if disk_gb * 1024**3 < destination.stat().st_size: raise ValueError('Disk shrinking is not supported')
        receipt_path = payload.get('_receipt_path')
        if not receipt_path: raise ValueError('Resize requires a durable operation identity')
        receipt = json.loads(receipt_path.read_text())
        prepared = receipt.get('prepared')
        temporary = destination.with_suffix('.resize')
        if prepared:
            if prepared.get('sha256'):
                if temporary.exists():
                    if self.file_digest(temporary) != prepared['sha256']: raise ValueError('Prepared resize checksum mismatch')
                    temporary.replace(destination); self.sync_directory(destination.parent)
                elif self.file_digest(destination) != prepared['sha256']:
                    raise ValueError('Interrupted resize could not establish disk commit')
            data.update(prepared['resources'])
            write_json(self.folder(identifier) / 'machine.json', data); self.sync_directory(self.folder(identifier))
            return prepared['result']
        resources = {'vcpus': cpus, 'memory_mb': memory, 'disk_gb': disk_gb}
        result = {'id': identifier, 'status': 'stopped', 'disk_id': identifier, **resources}
        digest = None
        try:
            if disk_gb * 1024**3 > destination.stat().st_size:
                if not self.capabilities()['resize_disk_grow']: raise ValueError('Disk growth is unsupported on this host')
                subprocess.run(['cp', '--reflink=auto', '--sparse=always', str(destination), str(temporary)], check=True, capture_output=True, timeout=900, **self.storage_subprocess_options())
                with temporary.open('r+b') as stream: stream.truncate(disk_gb * 1024**3); stream.flush(); os.fsync(stream.fileno())
                check = subprocess.run(['e2fsck', '-fp', str(temporary)], capture_output=True, timeout=300, **self.storage_subprocess_options())
                if check.returncode not in (0, 1, 2): raise ValueError('Resize staging filesystem failed repair')
                subprocess.run(['resize2fs', str(temporary)], check=True, capture_output=True, timeout=900, **self.storage_subprocess_options())
                check = subprocess.run(['e2fsck', '-fn', str(temporary)], capture_output=True, timeout=300, **self.storage_subprocess_options())
                if check.returncode != 0: raise ValueError('Resize staging filesystem failed strict validation')
                with temporary.open('rb') as stream: os.fsync(stream.fileno())
                digest = self.file_digest(temporary)
            self.require_stopped(identifier)
            receipt = json.loads(receipt_path.read_text())
            receipt['prepared'] = {'sha256': digest, 'resources': resources, 'result': result}
            self.save_storage_receipt(receipt_path, receipt)
            if digest:
                temporary.replace(destination); self.sync_directory(destination.parent)
            data.update(resources); write_json(self.folder(identifier) / 'machine.json', data); self.sync_directory(self.folder(identifier))
            return result
        finally:
            if not json.loads(receipt_path.read_text()).get('prepared'): temporary.unlink(missing_ok=True)

    def snapshot_manifest(self, identifier, snapshot_id):
        folder = self.storage_path(identifier, snapshot_id)
        manifest = json.loads((folder / 'manifest.json').read_text())
        if manifest.get('id') != snapshot_id or not (folder / 'disk.gz').is_file():
            raise ValueError('Snapshot is not complete')
        return manifest

    def snapshot(self, identifier, payload):
        self.require_stopped(identifier)
        return self._snapshot_contents(identifier, payload)

    def _snapshot_contents(self, identifier, payload, guard=None, publish=True):
        """Private checkpoint hook: caller owns machine lock and pause proof.
        A callback checks its attempt/watchdog epoch throughout copying; a
        resumed/re-paused VM must never falsely qualify a mixed raw copy.
        """
        if guard: guard()
        snapshot_id = payload['snapshot_id']
        folder = self.storage_path(identifier, snapshot_id)
        if (folder / 'manifest.json').exists(): return self.snapshot_manifest(identifier, snapshot_id)
        self.create_storage_directory(folder)
        source = self.folder(identifier) / 'root.ext4'
        target = folder / 'disk.gz.partial'
        digest = hashlib.sha256()
        with source.open('rb') as disk, target.open('wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1, mtime=0) as compressed:
                for chunk in iter(lambda: disk.read(1024 * 1024), b''):
                    if guard: guard()
                    digest.update(chunk); compressed.write(chunk)
            raw.flush(); os.fsync(raw.fileno())
        if guard: guard()
        manifest = {'id': snapshot_id, 'format': 'mola-raw-gzip-v1', 'architecture': self.arch,
                    'image_ref': self.metadata(identifier).get('image_ref', getattr(self, 'default_image_ref', 'omarchy-agent:0.1.0')),
                    'size_bytes': source.stat().st_size, 'sha256': digest.hexdigest(),
                    'artifact_bytes': target.stat().st_size,
                    'artifact_sha256': self.file_digest(target), 'created_at': int(time.time())}
        if guard: guard()
        if publish: return self._publish_snapshot(identifier, payload, manifest, guard)
        write_json(folder / 'manifest.json.pending', manifest)
        self.sync_directory(folder)
        return manifest

    def _publish_snapshot(self, identifier, payload, manifest, guard=None):
        """Seal only after the checkpoint coordinator proves resume and thaw."""
        if guard: guard()
        folder = self.storage_path(identifier, payload['snapshot_id'])
        target = folder / 'disk.gz.partial'
        if not target.is_file():
            if (folder / 'manifest.json').exists(): return self.snapshot_manifest(identifier, payload['snapshot_id'])
            raise ValueError('Snapshot staging artifact is missing')
        if self.file_digest(target) != manifest['artifact_sha256'] or target.stat().st_size != manifest['artifact_bytes']:
            raise ValueError('Snapshot staging checksum mismatch')
        if guard: guard()
        target.replace(folder / 'disk.gz')
        write_json(folder / 'manifest.json', manifest)
        (folder / 'manifest.json.pending').unlink(missing_ok=True)
        self.sync_directory(folder)
        return manifest

    @staticmethod
    def file_digest(path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
        return digest.hexdigest()

    def restore_snapshot(self, identifier, payload):
        self.require_stopped(identifier)
        folder = self.storage_path(identifier, payload['snapshot_id'])
        manifest = self.snapshot_manifest(identifier, payload['snapshot_id'])
        self.validate_snapshot_manifest(identifier, manifest)
        receipt_path = payload.get('_receipt_path')
        if receipt_path:
            receipt = json.loads(receipt_path.read_text())
            prepared = receipt.get('prepared')
            if prepared:
                destination = self.folder(identifier) / 'root.ext4'
                temporary = destination.with_suffix('.restore')
                if temporary.exists():
                    if self.file_digest(temporary) != prepared['sha256']: raise ValueError('Prepared restore checksum mismatch')
                    self.require_stopped(identifier)
                    temporary.replace(destination)
                    self.sync_directory(destination.parent)
                elif self.file_digest(destination) != prepared['sha256']:
                    raise ValueError('Interrupted restore could not establish its commit')
                return prepared['result']
        if self.file_digest(folder / 'disk.gz') != manifest['artifact_sha256']:
            raise ValueError('Snapshot artifact checksum mismatch')
        destination = self.folder(identifier) / 'root.ext4'
        temporary = destination.with_suffix('.restore')
        digest, total = hashlib.sha256(), 0
        try:
            with gzip.open(folder / 'disk.gz', 'rb') as source, temporary.open('wb') as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > manifest['size_bytes']: raise ValueError('Snapshot exceeds declared disk size')
                    digest.update(chunk)
                    if chunk.strip(b'\0'): target.write(chunk)
                    else: target.seek(len(chunk), 1)
                target.truncate(total); target.flush(); os.fsync(target.fileno())
            if total != manifest['size_bytes'] or digest.hexdigest() != manifest['sha256']:
                raise ValueError('Snapshot disk checksum mismatch')
            if self.config.get('guest_agent_refresh', False):
                # The archive may contain an old daemon that cannot re-enrol
                # after credential rotation. Verify original snapshot bytes
                # first, then refresh only the operator-managed guest agent.
                subprocess.run(['python3', str(Path(__file__).with_name('refresh_guest_agent.py')),
                                '--image', self.selected_image(self.metadata(identifier))['path'], '--disk', str(temporary), '--architecture', self.arch],
                               check=True, capture_output=True, timeout=900, **self.storage_subprocess_options())
                with temporary.open('rb') as stream: os.fsync(stream.fileno())
            if payload.get('fork', False):
                subprocess.run(['python3', str(Path(__file__).with_name('sanitize_clone.py')),
                                '--disk', str(temporary)], check=True, capture_output=True, timeout=300, **self.storage_subprocess_options())
                with temporary.open('rb') as stream: os.fsync(stream.fileno())
            self.require_stopped(identifier)
            if receipt_path:
                result = {'id': identifier, 'snapshot_id': manifest['id'], 'status': 'stopped',
                          'snapshot_sha256': manifest['sha256'], 'guest_agent_refreshed': bool(self.config.get('guest_agent_refresh', False)),
                          'fork_identity_reset': bool(payload.get('fork', False))}
                prepared_digest = self.file_digest(temporary)
                with self.storage_state():
                    receipt = json.loads(receipt_path.read_text())
                    receipt['prepared'] = {'sha256': prepared_digest, 'result': result}
                    self.save_storage_receipt(receipt_path, receipt)
            temporary.replace(destination)
            self.sync_directory(destination.parent)
        finally:
            if not receipt_path or not json.loads(receipt_path.read_text()).get('prepared'):
                temporary.unlink(missing_ok=True)
        return {'id': identifier, 'snapshot_id': manifest['id'], 'status': 'stopped',
                'snapshot_sha256': manifest['sha256'], 'guest_agent_refreshed': bool(self.config.get('guest_agent_refresh', False)),
                'fork_identity_reset': bool(payload.get('fork', False))}

    def storage_subprocess_options(self):
        # A transform can outlive a terminated supervisor. On POSIX inherit its
        # open-file lease so a replacement supervisor cannot touch the staging
        # disk until that transform (and its disk subprocesses) has finished.
        if platform.system() != 'Windows' and hasattr(self, 'lease'):
            return {'pass_fds': (self.lease.fileno(),),
                    'env': {**os.environ, 'MOLA_STORAGE_LEASE_FD': str(self.lease.fileno())}}
        return {}

    @staticmethod
    def sync_directory(path):
        if platform.system() == 'Windows': return
        descriptor = os.open(path, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)

    def validate_snapshot_manifest(self, identifier, manifest):
        if not isinstance(manifest, dict): raise ValueError('Invalid snapshot manifest')
        expected_image = self.metadata(identifier).get('image_ref', getattr(self, 'default_image_ref', 'omarchy-agent:0.1.0'))
        snapshot_image = manifest.get('image_ref', getattr(self, 'default_image_ref', 'omarchy-agent:0.1.0'))
        if snapshot_image != expected_image: raise ValueError('Snapshot image does not match this machine')
        if manifest.get('format') != 'mola-raw-gzip-v1' or manifest.get('architecture') != self.arch:
            raise ValueError('Snapshot format or architecture mismatch')
        limit = (self.folder(identifier) / 'root.ext4').stat().st_size
        if type(manifest.get('size_bytes')) is not int or not 1 <= manifest['size_bytes'] <= limit:
            raise ValueError('Snapshot disk exceeds target allocation')
        if type(manifest.get('artifact_bytes')) is not int or not 1 <= manifest['artifact_bytes'] <= limit + 1024 * 1024 * 1024:
            raise ValueError('Snapshot artifact exceeds target allocation')
        for key in ['sha256', 'artifact_sha256']:
            if not isinstance(manifest.get(key), str) or not re.fullmatch('[0-9a-f]{64}', manifest[key]):
                raise ValueError('Invalid snapshot checksum')

    def import_snapshot(self, identifier, payload):
        snapshot_id, manifest = payload['snapshot_id'], payload['manifest']
        self.validate_snapshot_manifest(identifier, manifest)
        if manifest.get('id') != snapshot_id: raise ValueError('Snapshot identity mismatch')
        folder = self.storage_path(identifier, snapshot_id)
        if (folder / 'manifest.json').exists():
            if self.snapshot_manifest(identifier, snapshot_id) != manifest: raise ValueError('Snapshot already exists with different content')
            return {'offset': manifest['artifact_bytes'], 'complete': True}
        self.create_storage_directory(folder)
        intent = folder / 'import.json'
        if intent.exists() and json.loads(intent.read_text()) != manifest: raise ValueError('Import already holds a different manifest')
        write_json(intent, manifest)
        target = folder / 'upload.partial'
        target.touch(mode=0o600, exist_ok=True)
        with target.open('rb') as stream: os.fsync(stream.fileno())
        self.sync_directory(folder)
        return {'offset': target.stat().st_size, 'complete': False}

    def write_snapshot_chunk(self, identifier, payload):
        folder = self.storage_path(identifier, payload['snapshot_id'])
        manifest = json.loads((folder / 'import.json').read_text())
        offset = payload['offset']
        if type(offset) is not int or offset < 0: raise ValueError('Invalid chunk offset')
        encoded = payload.get('data')
        if not isinstance(encoded, str) or len(encoded) > SNAPSHOT_CHUNK_ENCODED_BYTES: raise ValueError('Chunk exceeds 8 MiB')
        chunk = base64.b64decode(encoded, validate=True)
        if not chunk or hashlib.sha256(chunk).hexdigest() != payload.get('sha256'): raise ValueError('Chunk checksum mismatch')
        if offset + len(chunk) > manifest['artifact_bytes']: raise ValueError('Chunk exceeds artifact size')
        if (folder / 'manifest.json').exists():
            target = folder / 'disk.gz'
        else: target = folder / 'upload.partial'
        with target.open('r+b') as stream:
            size = target.stat().st_size
            if offset > size: raise ValueError('Chunks must be uploaded sequentially')
            stream.seek(offset)
            if offset < size:
                if offset + len(chunk) > size or stream.read(len(chunk)) != chunk: raise ValueError('Retry chunk differs from stored bytes')
            else:
                stream.write(chunk); stream.flush(); os.fsync(stream.fileno())
        return {'offset': max(size, offset + len(chunk))}

    def seal_snapshot(self, identifier, payload):
        folder = self.storage_path(identifier, payload['snapshot_id'])
        if (folder / 'manifest.json').exists(): return self.snapshot_manifest(identifier, payload['snapshot_id'])
        manifest = json.loads((folder / 'import.json').read_text())
        target = folder / 'upload.partial'
        if not target.exists() and (folder / 'disk.gz').exists(): target = folder / 'disk.gz'
        if target.stat().st_size != manifest['artifact_bytes'] or self.file_digest(target) != manifest['artifact_sha256']:
            raise ValueError('Incomplete snapshot or artifact checksum mismatch')
        target.replace(folder / 'disk.gz')
        write_json(folder / 'manifest.json', manifest)
        self.sync_directory(folder)
        return manifest

    def read_snapshot_chunk(self, identifier, payload):
        manifest = self.snapshot_manifest(identifier, payload['snapshot_id'])
        offset = payload['offset']
        if type(offset) is not int or not 0 <= offset < manifest['artifact_bytes']: raise ValueError('Invalid chunk offset')
        with (self.storage_path(identifier, payload['snapshot_id']) / 'disk.gz').open('rb') as stream:
            stream.seek(offset); chunk = stream.read(SNAPSHOT_CHUNK_BYTES)
        return {'offset': offset, 'next_offset': offset + len(chunk), 'data': base64.b64encode(chunk).decode(),
                'sha256': hashlib.sha256(chunk).hexdigest(), 'eof': offset + len(chunk) == manifest['artifact_bytes']}

    def delete_snapshot(self, identifier, payload):
        folder = self.storage_path(identifier, payload['snapshot_id'])
        if folder.exists(): shutil.rmtree(folder)
        if folder.parent.exists(): self.sync_directory(folder.parent)
        return {'id': payload['snapshot_id'], 'deleted': True}

    def fence(self, identifier, payload):
        data = self.metadata(identifier)
        data['fenced'] = True
        write_json(self.folder(identifier) / 'machine.json', data)
        self.sync_directory(self.folder(identifier))
        self.stop(identifier, force=True)
        for _ in range(100):
            if self.status(self.metadata(identifier)) == 'stopped': return {'id': identifier, 'fenced': True, 'status': 'stopped'}
            time.sleep(0.1)
        raise ValueError('Fencing has not established stopped state')

    def storage_incarnation(self, identifier):
        data = self.metadata(identifier)
        # Existing disks predate receipt namespaces. Their immutable create
        # fingerprint identifies that incarnation; new creates get a random ID.
        incarnation = data.get('storage_incarnation') or data.get('create_fingerprint') or 'legacy'
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', incarnation): raise ValueError('Invalid storage incarnation')
        return incarnation

    def storage_journal(self, identifier):
        folder = self.root / 'storage-operations' / identifier / self.storage_incarnation(identifier)
        if any(path.is_symlink() for path in (folder, folder.parent, folder.parent.parent)): raise ValueError('Unsafe storage journal')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sync_directory(folder.parent)
        self.sync_directory(folder.parent.parent)
        self.sync_directory(self.root)
        return folder

    def operation_path(self, identifier, verb, operation_id):
        if verb not in DURABLE_STORAGE_VERBS or not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
            raise ValueError('Invalid storage operation identity')
        return self.storage_journal(identifier) / (verb + '-' + operation_id + '.json')

    def storage_state(self):
        # Tiny test runners omit process setup. Initialization happens before
        # their HTTP server starts; production initializes in the constructor.
        if not hasattr(self, 'storage_guard'):
            self.storage_guard, self.storage_workers = threading.RLock(), {}
        return self.storage_guard

    def save_storage_receipt(self, path, receipt):
        write_json(path, receipt)
        self.sync_directory(path.parent)

    def storage_receipt(self, identifier, verb, operation_id):
        path = self.operation_path(identifier, verb, operation_id)
        with self.storage_state():
            receipt = json.loads(path.read_text())
            # Never expose the persisted request or stage paths on the wire.
            return {key: receipt[key] for key in ('operation_id', 'verb', 'generation', 'status', 'result', 'error', 'error_status') if key in receipt}

    def submit_storage(self, identifier, verb, payload):
        operation_id, generation = payload.get('operation_id'), payload.get('generation')
        if type(generation) is not int or generation < 1: raise ValueError('Invalid storage generation')
        path = self.operation_path(identifier, verb, operation_id)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.storage_state():
            data = self.metadata(identifier)
            if path.exists():
                receipt = json.loads(path.read_text())
                if receipt['fingerprint'] != fingerprint: raise StorageConflict('Operation ID was already used with a different payload')
            else:
                if generation < data.get('storage_generation', 0): raise StorageConflict('Stale storage generation')
                for previous in path.parent.glob('*.json'):
                    prior = json.loads(previous.read_text())
                    if generation < prior['generation']: raise StorageConflict('Stale storage generation')
                    if prior['status'] == 'pending':
                        raise StorageConflict('A pending storage operation must be reconciled first')
                if verb in ('snapshot', 'restore', 'resize'): self.require_stopped(identifier)
                receipt = {'operation_id': operation_id, 'verb': verb, 'generation': generation,
                           'fingerprint': fingerprint, 'incarnation': self.storage_incarnation(identifier), 'payload': payload, 'status': 'pending', 'created_at': time.time()}
                # The journal is the source of truth for an accepted request;
                # persist it before scheduling any work or acknowledging it.
                self.save_storage_receipt(path, receipt)
                data['storage_generation'] = generation
                write_json(self.folder(identifier) / 'machine.json', data)
                self.sync_directory(self.folder(identifier))
            if receipt['status'] == 'pending': self.launch_storage_worker(identifier, path)
            return self.storage_receipt(identifier, verb, operation_id)

    def launch_storage_worker(self, identifier, path):
        key = str(path)
        if key in self.storage_workers and self.storage_workers[key].is_alive(): return
        worker = threading.Thread(target=self.run_storage_worker, args=(identifier, path), daemon=True)
        self.storage_workers[key] = worker
        worker.start()

    def recover_storage_operations(self):
        with self.storage_state():
            root = self.root / 'storage-operations'
            if not root.exists(): return
            for folder in root.iterdir():
                if not ID.fullmatch(folder.name): continue
                try: incarnation = self.storage_incarnation(folder.name)
                except FileNotFoundError: continue # Deleted incarnation receipts are tombstones.
                for path in (folder / incarnation).glob('*.json'):
                    receipt = json.loads(path.read_text())
                    if receipt['status'] == 'pending' and receipt['incarnation'] == incarnation:
                        self.launch_storage_worker(folder.name, path)

    def require_no_pending_storage(self, identifier):
        with self.storage_state():
            try: folder = self.storage_journal(identifier)
            except FileNotFoundError: return
            if folder.exists() and any(json.loads(path.read_text())['status'] == 'pending' for path in folder.glob('*.json')):
                raise StorageConflict('A pending storage operation must be reconciled first')

    def run_storage_worker(self, identifier, path):
        # No global runner lock is held during compression, verification or
        # restore. Same-machine lifecycle work uses this same mutation lock.
        with self.machine_lock(identifier):
            with self.storage_state(): receipt = json.loads(path.read_text())
            if receipt['status'] != 'pending': return
            try:
                if receipt['incarnation'] != self.storage_incarnation(identifier): raise StorageConflict('Operation belongs to a retired disk incarnation')
                payload = dict(receipt['payload'])
                if receipt['verb'] in ('restore', 'resize', 'checkpoint'): payload['_receipt_path'] = path
                result = self.storage_operation(identifier, receipt['verb'], payload)
                with self.storage_state():
                    receipt = json.loads(path.read_text())
                    receipt.update(status='completed', result=result, completed_at=time.time())
                    self.save_storage_receipt(path, receipt)
            except Exception as error:
                if isinstance(error, _checkpoint_module.CheckpointRecoveryPending):
                    # Guardians may still be releasing the frozen guest. The
                    # original receipt remains authoritative and owns admission.
                    return
                if isinstance(error, subprocess.TimeoutExpired) and platform.system() != 'Windows' and hasattr(self, 'lease'):
                    # subprocess.run kills only its direct helper on timeout.
                    # A nested writer can still own a staging disk. Leave the
                    # accepted journal pending and fail closed by terminating
                    # this supervisor: none of its threads can admit another
                    # restore, and inherited leases exclude a replacement until
                    # every surviving writer exits. Recovery resumes unfinished
                    # preparation only after a fresh supervisor acquires lease.
                    os._exit(75)
                with self.storage_state():
                    receipt = json.loads(path.read_text())
                    if receipt.get('prepared') and isinstance(error, OSError):
                        # The swap may have committed. Preserve its evidence and
                        # allow an exact retry to verify the disk instead of
                        # claiming a failure or repeating decompression.
                        return
                    receipt.update(status='failed', error=str(error) if isinstance(error, (ValueError, FileNotFoundError)) else 'Native storage operation failed; inspect host logs',
                                   error_status=409 if isinstance(error, StorageConflict) else 422 if isinstance(error, (ValueError, KeyError, TypeError)) else 404 if isinstance(error, FileNotFoundError) else 503,
                                   completed_at=time.time())
                    self.save_storage_receipt(path, receipt)
                print('Storage operation failed: ' + type(error).__name__, flush=True)

    def storage_operation(self, identifier, verb, payload):
        operations = {'snapshot': self.snapshot, 'checkpoint': self.checkpoint, 'restore': self.restore_snapshot,
                      'snapshot-delete': self.delete_snapshot, 'snapshot-import': self.import_snapshot,
                      'snapshot-write': self.write_snapshot_chunk, 'snapshot-seal': self.seal_snapshot,
                      'snapshot-read': self.read_snapshot_chunk, 'fence': self.fence, 'resize': self.resize}
        return operations[verb](identifier, payload)

    def list(self):
        result = {}
        for folder in self.machines.iterdir():
            if ID.fullmatch(folder.name) and (folder / 'machine.json').exists():
                try: result[folder.name] = self.describe(folder.name)['status']
                except FileNotFoundError:
                    # A concurrent completed destroy can remove metadata between
                    # directory enumeration and acquiring that machine's lock.
                    pass
        return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # No request bodies or credentials in logs.

    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch()
    def do_DELETE(self): self.dispatch()

    def dispatch(self):
        try:
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
                return self.reply(401, {'error': 'Unauthorized'})
            if self.headers.get('Origin'):
                return self.reply(403, {'error': 'Browser origins are not accepted'})
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > request_body_limit(self.path, self.command):
                return self.reply(413, {'error': 'Request too large'})
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(size)) if size else {}
            parts = self.path.strip('/').split('/')
            runner = self.server.runner
            # Storage compression may hold the mutation lock for minutes. A
            # responsive supervisor must not look dead during that work.
            if self.command == 'GET' and parts == ['health']:
                return self.reply(200, {'ready': True, 'architecture': runner.arch, 'accelerator': runner.accel})
            if self.command == 'GET' and parts == ['capabilities']:
                return self.reply(200, runner.capabilities())
            if self.command == 'GET' and parts == ['machines']:
                return self.reply(200, runner.list())
            if self.command == 'GET' and len(parts) == 2 and parts[0] == 'machines':
                return self.reply(200, runner.describe(parts[1]))
            if self.command == 'GET' and len(parts) == 5 and parts[0] == 'machines' and parts[2] == 'storage-operations':
                receipt = runner.storage_receipt(parts[1], parts[3], parts[4])
                return self.reply(202 if receipt['status'] == 'pending' else 200, {'storage_operation': receipt})
            if self.command == 'POST' and len(parts) == 3 and parts[0] == 'machines' and parts[2] in DURABLE_STORAGE_VERBS and 'operation_id' in payload:
                receipt = runner.submit_storage(parts[1], parts[2], payload)
                return self.reply(202 if receipt['status'] == 'pending' else 200, {'storage_operation': receipt})
            identifier = payload.get('computer_id') if parts == ['machines'] and self.command == 'POST' else (parts[1] if len(parts) >= 2 and parts[0] == 'machines' else None)
            if identifier is not None and self.command in ('POST', 'DELETE'):
                runner.require_no_pending_storage(identifier)
            machine_lock = runner.machine_lock(identifier) if identifier is not None else threading.RLock()
            # Keep host resource allocation serialized while readers for other
            # computers remain responsive during a large snapshot or restore.
            with machine_lock, runner.lock:
                if identifier is not None and self.command in ('POST', 'DELETE'):
                    runner.require_no_pending_storage(identifier)
                if parts == ['machines'] and self.command == 'POST': result = runner.create(payload)
                elif len(parts) == 4 and parts[0] == 'machines' and parts[2] == 'snapshots' and self.command == 'GET': result = runner.snapshot_manifest(parts[1], parts[3])
                elif len(parts) == 2 and parts[0] == 'machines' and self.command == 'GET': result = runner.describe(parts[1])
                elif len(parts) == 2 and parts[0] == 'machines' and self.command == 'DELETE':
                    if type(payload.get('delete_disk')) is not bool: raise ValueError('delete_disk must be explicitly true or false')
                    runner.destroy(parts[1], payload['delete_disk']); result = {'ok': True}
                elif len(parts) == 3 and parts[0] == 'machines' and self.command == 'POST':
                    if parts[2] in ('snapshot', 'restore', 'snapshot-delete', 'snapshot-import', 'snapshot-write', 'snapshot-seal', 'snapshot-read', 'fence'):
                        result = runner.storage_operation(parts[1], parts[2], payload)
                    elif parts[2] == 'reseed': result = runner.reseed(parts[1], payload)
                    elif parts[2] == 'start': result = runner.start(parts[1])
                    elif parts[2] in ('shutdown', 'force-stop'):
                        runner.stop(parts[1], parts[2] == 'force-stop'); result = {'ok': True}
                    else: return self.reply(404, {'error': 'Unknown operation'})
                else: return self.reply(404, {'error': 'Unknown operation'})
            self.reply(200, result)
        except StorageConflict as error: self.reply(409, {'error': str(error)})
        except FileNotFoundError: self.reply(404, {'error': 'Machine or runtime artifact not found'})
        except (ValueError, KeyError, TypeError) as error: self.reply(422, {'error': str(error)})
        except Exception as error:
            print(type(error).__name__ + ': ' + str(error), flush=True)
            self.reply(503, {'error': 'Native runtime failed; inspect host and machine logs'})

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def runtime_lease(root):
    """A restarted daemon must not overlap workers from an older supervisor."""
    lease = (root / 'runtime.lock').open('a+b')
    try:
        if platform.system() == 'Windows':
            import msvcrt
            if lease.seek(0, 2) == 0: lease.write(b'0'); lease.flush()
            lease.seek(0)
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lease
    except OSError:
        lease.close()
        raise ValueError('Another native supervisor already owns this runtime state')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=19380)
    args = parser.parse_args()
    os.umask(0o077)
    lease = runtime_lease(args.config.resolve().parent)
    runner = Runner(args.config)
    runner.lease = lease
    runner.recover_storage_operations()
    token_path = runner.root / 'host.token'
    if not token_path.exists():
        with token_path.open('x') as stream: stream.write(secrets.token_hex(32))
    token = token_path.read_text().strip()
    if len(token) < 32: raise ValueError('Host token must contain at least 32 characters')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    server.runner, server.token = runner, token
    print(f'Mola native host listening on 127.0.0.1:{args.port} ({runner.accel}/{runner.arch})', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        # Keep the exclusive lease until the supervisor process exits; daemon
        # workers are terminated with it, then the OS releases the file lock.
    # Existing VMs retain their QMP sockets and disks. Restarting this service
    # reconnects to them; it never guesses ownership from an operating-system PID.


if __name__ == '__main__': main()
