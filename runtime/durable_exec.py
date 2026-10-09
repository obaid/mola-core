"""Private durable exec. Command/payload live only in stdin and anonymous RAM.

Receipts retain binding, host-keyed digest and sanitized terminal output only.
A detached systemd user scope owns all ordinary descendants. Disappearance is
unknown unless a completed worker receipt and an empty original scope prove
termination. Accepted operations never launch a second worker.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid

MAX_OUTPUT=65536
MAX_RAW_OUTPUT=1048576
TERMINAL={'completed','failed'}
BOOTSTRAP="import json,sys;p=json.load(sys.stdin);n={};exec(p['source'],n);n['worker'](p['request'])"

class ExecError(Exception):
    def __init__(self,code,status=409):self.code=code;self.status=status

def require(value,code='invalid_exec_arguments',status=400):
    if not value:raise ExecError(code,status)

def boot_id():return str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))

def folder():
    base=Path.home()/'.local/state/mola'
    for path in [Path.home(),Path.home()/'.local',Path.home()/'.local/state',base,base/'executions']:
        if path!=Path.home():path.mkdir(mode=0o700,exist_ok=True)
        info=path.lstat()
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid==os.getuid()
                and not info.st_mode&0o002 and (path not in [base,base/'executions'] or not info.st_mode&0o077),'unsafe_exec_directory',501)
    return base/'executions'

def operation(arguments):
    identifier=arguments.get('operation_id')
    require(isinstance(identifier,str) and re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}',identifier))
    generation=arguments.get('expected_generation');boot=arguments.get('expected_boot_id')
    require(type(generation) is int and 1<=generation<=9007199254740991 and isinstance(boot,str)
            and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',boot))
    return identifier.lower(),generation,boot

def spec(arguments):
    command=arguments.get('command');timeout=arguments.get('timeout_seconds',30)
    payload=arguments.get('payload',{});redact=arguments.get('redact',[])
    require(isinstance(command,str) and 0<len(command.encode())<=32768 and '\0' not in command)
    require(type(timeout) is int and 1<=timeout<=900 and isinstance(payload,dict)
            and len(json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode())<=1048576)
    require(isinstance(redact,list) and len(redact)<=20 and all(isinstance(v,str) and 4<=len(v.encode())<=4096 for v in redact))
    digest=arguments.get('payload_digest')
    require(isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest),'exec_digest_required')
    return {'command':command,'timeout_seconds':timeout,'payload':payload,'redact':redact}

def path_for(identifier):return folder()/(identifier+'.json')

def load(path):
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return None
    with os.fdopen(descriptor) as stream:
        info=os.fstat(stream.fileno());require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077 and info.st_size<=262144,'unsafe_exec_receipt',501)
        return json.load(stream)

def save(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.new')
    try:
        with open(temporary,'x',opener=lambda p,flags:os.open(p,flags|os.O_NOFOLLOW,0o600)) as stream:
            json.dump(value,stream,separators=(',',':'),ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        try:temporary.unlink()
        except FileNotFoundError:pass

class Lock:
    def __init__(self,path):self.path=path.with_suffix('.lock')
    def __enter__(self):
        self.fd=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        info=os.fstat(self.fd);require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077,'unsafe_exec_receipt',501)
        fcntl.flock(self.fd,fcntl.LOCK_EX);return self
    def __exit__(self,*_):fcntl.flock(self.fd,fcntl.LOCK_UN);os.close(self.fd)

def private_input(value,name='mola-private-exec'):
    require(hasattr(os,'memfd_create'),'exec_scope_unsupported',501)
    descriptor=os.memfd_create(name,getattr(os,'MFD_CLOEXEC',1))
    try:
        data=value if isinstance(value,bytes) else value.encode()
        position=0
        while position<len(data):position+=os.write(descriptor,data[position:])
        os.lseek(descriptor,0,os.SEEK_SET);return descriptor
    except Exception:os.close(descriptor);raise

def manager_environment():
    env=dict(os.environ);env.setdefault('XDG_RUNTIME_DIR','/run/user/'+str(os.getuid()))
    env.setdefault('DBUS_SESSION_BUS_ADDRESS','unix:path='+env['XDG_RUNTIME_DIR']+'/bus')
    return env

def capabilities():
    supported=hasattr(os,'memfd_create') and Path('/sys/fs/cgroup/cgroup.controllers').is_file()
    try:boot_id()
    except (OSError,ValueError):supported=False
    if supported:
        try:
            result=subprocess.run(['/usr/bin/systemctl','--user','is-system-running'],capture_output=True,timeout=5,env=manager_environment())
            supported=result.stdout.strip() in [b'running',b'degraded'] and result.returncode in [0,1]
        except (OSError,subprocess.SubprocessError):supported=False
    return {'supported':bool(supported),'max_timeout_seconds':900 if supported else 0}

def process_state(pid,start):
    try:
        value=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        return value[19]==start and value[0] not in ['Z','X']
    except FileNotFoundError:return False
    except (OSError,ValueError,IndexError):return None

def scope_path(record):
    value=record.get('scope')
    require(isinstance(record.get('attempt'),str) and re.fullmatch(r'[a-f0-9]{32}',record['attempt'])
            and record.get('unit')=='mola-exec-'+record['attempt']+'.service'
            and isinstance(value,str) and value.startswith('/user.slice/user-'+str(os.getuid())+'.slice/user@'+str(os.getuid())+'.service/') and value.endswith('/'+record['unit'])
            and not any(v in ['.','..'] for v in value.split('/')),'exec_scope_unknown',501)
    return Path('/sys/fs/cgroup')/value.lstrip('/')

def scope_gone(record):
    try:
        if boot_id()!=record['guest_boot_id']:return False
        if process_state(record.get('worker_pid',0),record.get('worker_start','')) is not False:return False
        path=scope_path(record)
        try:path.stat()
        except FileNotFoundError:return True
        events=(path/'cgroup.events').read_text().splitlines()
        return 'populated 0' in events
    except (OSError,ValueError,KeyError,ExecError):return False

def public(record):
    result={'operation_id':record['operation_id'],'generation':record['generation'],'boot_id':record['boot_id'],
            'status':record['status'] if record['status'] in TERMINAL|{'queued','running','outcome_unknown'} else 'running',
            'terminal':record['status'] in TERMINAL}
    if result['terminal']:result['result']=record['result']
    return result

def same_binding(record,arguments):
    identifier,generation,boot=operation(arguments)
    require((record['operation_id'],record['generation'],record['boot_id'])==(identifier,generation,boot),'exec_binding_mismatch')

def reconcile(path,record):
    if record['status'] in TERMINAL:return record
    if record['status']=='finishing' and scope_gone(record):
        record['status']='completed' if record['result']['exit_code']==0 else 'failed'
        record['finished_at']=time.time();save(path,record)
    elif record.get('cancel_requested_at') is not None and record['status']=='running' and scope_gone(record):
        record.update(status='failed',finished_at=time.time(),result={'exit_code':130,'stdout':'','stderr':'','cancelled':True});save(path,record)
    elif record['guest_boot_id']!=boot_id() or record['status']=='queued' and time.time()-record['accepted_at']>15 or record['status']=='running' and process_state(record.get('worker_pid',0),record.get('worker_start','')) is False:
        record['status']='outcome_unknown';save(path,record)
    return record

def status(arguments):
    identifier,_,_=operation(arguments);path=path_for(identifier)
    with Lock(path):
        record=load(path);require(record is not None,'exec_operation_not_found',404)
        same_binding(record,arguments);return public(reconcile(path,record))

def submit(arguments,binding,source):
    identifier,generation,boot=operation(arguments);definition=spec(arguments)
    require(isinstance(source,str) and source,'exec_source_unavailable',501)
    path=path_for(identifier)
    with Lock(path):
        record=load(path)
        if record is not None:
            same_binding(record,arguments);require(record['payload_digest']==arguments['payload_digest'],'exec_payload_mismatch')
            return public(reconcile(path,record))
        require(binding.get('generation')==generation and binding.get('boot_id')==boot and boot==boot_id(),'exec_binding_mismatch')
        require(capabilities()['supported'],'exec_scope_unsupported',501)
        record={'operation_id':identifier,'generation':generation,'boot_id':boot,'guest_boot_id':boot_id(),
                'payload_digest':arguments['payload_digest'],'attempt':uuid.uuid4().hex,'status':'queued','accepted_at':time.time()}
        record['unit']='mola-exec-'+record['attempt']+'.service'
        save(path,record)
        request={'arguments':arguments,'binding':binding,'attempt':record['attempt']}
        descriptor=private_input(json.dumps({'source':source,'request':request},separators=(',',':'),ensure_ascii=False))
        try:
            subprocess.Popen(['/usr/bin/systemd-run','--user','--quiet','--wait','--pipe','--unit='+record['unit'],
                '--property=Type=exec','--property=KillMode=control-group','--property=TimeoutStopSec=5s',
                '--property=RuntimeMaxSec='+str(definition['timeout_seconds']+15)+'s',
                '/usr/bin/python3','-c',BOOTSTRAP],stdin=descriptor,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                env=manager_environment(),start_new_session=True,close_fds=True)
        except (OSError,subprocess.SubprocessError):
            record['status']='outcome_unknown';save(path,record)
        finally:os.close(descriptor)
        return public(record)

def cancel(arguments):
    identifier,_,_=operation(arguments);path=path_for(identifier)
    with Lock(path):
        record=load(path);require(record is not None,'exec_operation_not_found',404);same_binding(record,arguments)
        record=reconcile(path,record)
        if record['status'] in TERMINAL:return public(record)
        if record['guest_boot_id']!=boot_id() or record['status']=='outcome_unknown':return public({**record,'status':'outcome_unknown'})
        record['cancel_requested_at']=time.time()
        if record['status']=='queued':
            # The same receipt lock guards the worker's launch fence. A late
            # supervisor can start, but can never launch this command now.
            record.update(status='failed',finished_at=time.time(),result={'exit_code':130,'stdout':'','stderr':'','cancelled':True});save(path,record)
            return public(record)
        scope_path(record);save(path,record);unit=record['unit']
    try:subprocess.run(['/usr/bin/systemctl','--user','stop','--',unit],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,env=manager_environment())
    except (OSError,subprocess.SubprocessError):pass
    return status(arguments)

def declared_secrets(definition):
    secrets=list(definition['redact'])
    pending=[(definition['payload'],False)]
    while pending:
        value,sensitive=pending.pop()
        if isinstance(value,dict):
            for key,item in value.items():
                pending.append((item,sensitive or bool(re.search(r'password|secret|token|authorization|cookie|api[_-]?key',str(key),re.I))))
        elif isinstance(value,list):
            pending.extend((item,sensitive) for item in value)
        elif sensitive and isinstance(value,(str,int,float)) and str(value):secrets.append(str(value))
    variants=set(secrets)
    for value in secrets:
        variants.add(json.dumps(value,ensure_ascii=True)[1:-1]);variants.add(json.dumps(value,ensure_ascii=False)[1:-1])
    return sorted((value for value in variants if value),key=len,reverse=True)

def sanitized_output(raw,definition,overflow,limit=MAX_OUTPUT):
    if overflow:return '[OUTPUT OMITTED: SIZE LIMIT]'
    text=raw.decode('utf8','replace')
    for secret in declared_secrets(definition):text=text.replace(secret,'[REDACTED]')
    text=re.sub(r'(?i)([\"\']?(?:authorization|proxy-authorization|cookie|set-cookie)[\"\']?\s*[:=]\s*)[^\n]*',r'\1[REDACTED]',text)
    text=re.sub(r'(?i)(["\']?(?:password|passwd|secret|access[_-]?token|api[_-]?key|authorization|cookie)["\']?\s*[:=]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;]+)',r'\1[REDACTED]',text)
    text=''.join(c for c in text if c in '\n\t' or ord(c)>=32 and ord(c)!=127)
    return text.encode()[:limit].decode('utf8','ignore')

def command_result(definition):
    script=private_input(definition['command']+'\n','mola-exec-script')
    payload=private_input(json.dumps(definition['payload'],separators=(',',':'),ensure_ascii=False)+'\n','mola-exec-payload')
    child=None;raw={'stdout':bytearray(),'stderr':bytearray()};overflow=False;timed_out=False
    try:
        child=subprocess.Popen(['/bin/bash','--noprofile','--norc','/proc/self/fd/'+str(script)],stdin=payload,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(script,),start_new_session=True,
                               env=dict(os.environ,MOLA_JOB_RUN_ID=definition.get('operation_id','')))
        reader=selectors.DefaultSelector()
        for channel in ['stdout','stderr']:
            stream=getattr(child,channel);os.set_blocking(stream.fileno(),False);reader.register(stream,selectors.EVENT_READ,channel)
        deadline=time.monotonic()+definition['timeout_seconds'];drain_deadline=None
        try:
            while True:
                for key,_ in reader.select(0.1):
                    try:data=os.read(key.fd,8192)
                    except BlockingIOError:continue
                    if data:
                        if not overflow:
                            if sum(len(value) for value in raw.values())+len(data)>MAX_RAW_OUTPUT:
                                for value in raw.values():value.clear()
                                overflow=True
                            else:raw[key.data].extend(data)
                    else:reader.unregister(key.fileobj)
                code=child.poll()
                if code is not None:
                    if drain_deadline is None:drain_deadline=time.monotonic()+1
                    if not reader.get_map() or time.monotonic()>=drain_deadline:break
                elif time.monotonic()>=deadline:
                    timed_out=True;os.killpg(child.pid,signal.SIGKILL);child.wait()
            code=child.wait();result={'exit_code':124 if timed_out else code,
                'stdout':sanitized_output(raw['stdout'],definition,overflow,MAX_OUTPUT//2),
                'stderr':sanitized_output(raw['stderr'],definition,overflow,MAX_OUTPUT//2)}
            if timed_out:result['timed_out']=True
            return result
        finally:reader.close();child.stdout.close();child.stderr.close()
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid,signal.SIGKILL);child.wait()
        os.close(script);os.close(payload)

def worker(request):
    arguments=request['arguments'];identifier,generation,boot=operation(arguments);definition=spec(arguments);definition['operation_id']=identifier;path=path_for(identifier)
    with Lock(path):
        record=load(path)
        require(record is not None,'exec_operation_not_found');same_binding(record,arguments)
        require(record['attempt']==request['attempt'] and record['payload_digest']==arguments['payload_digest']
                and record['status']=='queued' and not record.get('cancel_requested_at') and record['guest_boot_id']==boot_id()
                and boot==boot_id() and time.time()-record['accepted_at']<=15,'exec_launch_fenced')
        pid=os.getpid();start=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]
        groups=Path('/proc/self/cgroup').read_text().splitlines();scope=next((v[3:] for v in groups if v.startswith('0::')),None)
        record.update(worker_pid=pid,worker_start=start,scope=scope)
        scope_path(record)
        record.update(status='running',started_at=time.time());save(path,record)
    try:result=command_result(definition)
    except Exception:result={'exit_code':125,'stdout':'','stderr':''}
    with Lock(path):
        current=load(path)
        if current is None or current['attempt']!=record['attempt'] or current['status']!='running':return
        current.update(status='finishing',result=result,command_ended_at=time.time());save(path,current)
    # systemd terminates any ordinary background descendants when this main
    # worker exits. Status publishes terminal only after that scope is empty.

def dispatch(tool,arguments,binding,source):
    if tool=='exec_capabilities':return capabilities()
    if tool=='exec_submit':return submit(arguments,binding,source)
    if tool=='exec_status':return status(arguments)
    if tool=='exec_cancel':return cancel(arguments)
    raise ExecError('unsupported_exec_tool',400)
