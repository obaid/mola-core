#!/usr/bin/env python3
"""Private persistent transport worker. Requests are JSONL; payloads are never logged."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

GUEST_PROGRAM = r'''
import base64, json, os, pathlib, signal, stat, subprocess, sys, tempfile
p=json.load(sys.stdin)
a=p['action']
limit=1048576
if a=='exec':
    runtime=pathlib.Path('/run/user')/str(os.getuid())
    for candidate in sorted(runtime.glob('wayland-*')):
        if stat.S_ISSOCK(candidate.stat().st_mode):
            os.environ.update(XDG_RUNTIME_DIR=str(runtime),WAYLAND_DISPLAY=candidate.name,
                              XDG_SESSION_TYPE='wayland',XDG_CURRENT_DESKTOP='Hyprland')
            if (runtime/'bus').exists():
                os.environ['DBUS_SESSION_BUS_ADDRESS']='unix:path='+str(runtime/'bus')
            break
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        child=subprocess.Popen(['/bin/bash','-lc',p['command']],stdout=out,stderr=err,start_new_session=True,cwd=os.path.expanduser('~'))
        timed_out=False
        try: child.wait(timeout=p.get('timeout',30))
        except subprocess.TimeoutExpired:
            timed_out=True
            os.killpg(child.pid,signal.SIGKILL)
            child.wait()
        out.seek(0); err.seek(0)
        stdout=out.read(limit+1); stderr=err.read(limit+1)
        print(json.dumps(dict(exit_code=child.returncode,stdout=stdout[:limit].decode('utf8','replace'),stderr=stderr[:limit].decode('utf8','replace'),timed_out=timed_out,truncated=len(stdout)>limit or len(stderr)>limit)))
elif a=='read_file':
    path=pathlib.Path(p['path']).expanduser()
    with path.open('rb') as f: data=f.read(limit+1)
    if len(data)>limit: raise ValueError('File exceeds 1 MiB; use exec to select a smaller portion.')
    print(json.dumps(dict(content_base64=base64.b64encode(data).decode(),size=len(data))))
elif a=='write_file':
    path=pathlib.Path(p['path']).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    data=base64.b64decode(p['content_base64']) if 'content_base64' in p else p['content'].encode()
    if len(data)>limit: raise ValueError('File exceeds 1 MiB.')
    path.write_bytes(data)
    print(json.dumps(dict(path=str(path),bytes_written=len(data))))
'''

class ActionError(RuntimeError):
    def __init__(self, code):
        super().__init__('Automation transport failed.')
        self.code = code

class Worker:
    def __init__(self):
        self.displays = {}

    def _prune_display(self, machine_id, keep):
        for key in list(self.displays):
            if key[0] == machine_id and key != keep:
                client = self.displays.pop(key)
                try: client.disconnect()
                except Exception: pass

    def _ssh(self, target, action):
        started = time.perf_counter_ns()
        host = target['ssh_host']
        port = int(target['ssh_port'])
        if not host or host.startswith('-') or not 0 < port < 65536:
            raise ValueError('Invalid connection target')
        control = Path(target['control_dir'])
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256((target['id'] + ':' + target['session_key']).encode()).hexdigest()[:32]
        socket = control / ('ssh-' + digest)
        reused = socket.exists()
        encoded = base64.b64encode(GUEST_PROGRAM.encode()).decode()
        remote = "python3 -c \"import base64; exec(base64.b64decode('" + encoded + "'))\""
        # The host alias remains stable for the computer, preserving the
        # existing host-key trust boundary. The control socket is boot-scoped.
        alias = 'mola-' + target['id']
        args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ControlMaster=auto', '-o', 'ControlPersist=60',
                '-o', 'ControlPath=' + str(socket), '-o', 'HostKeyAlias=' + alias,
                '-o', 'UserKnownHostsFile=' + target['known_hosts'],
                '-i', target['ssh_key'], '-p', str(port), 'dev@' + host, remote]
        try:
            result = subprocess.run(args, input=json.dumps(action), capture_output=True, text=True,
                                    timeout=action.get('timeout', 30) + 15)
        except subprocess.TimeoutExpired as error:
            raise ActionError('action_outcome_unknown') from error
        if result.returncode:
            # OpenSSH cannot prove whether a transport failure happened before
            # or after the guest received a side effect. Never replay it.
            raise ActionError('action_outcome_unknown' if action['action'] in ('exec','write_file') else 'transport_failed')
        try: value = json.loads(result.stdout)
        except Exception as error: raise ActionError('invalid_guest_response') from error
        return value, {'ssh_ms': round((time.perf_counter_ns()-started)/1_000_000, 3), 'ssh_reused': reused}

    def _display(self, target, action):
        started = time.perf_counter_ns()
        from vncdotool import api
        from vncdotool.client import VNCDoToolFactory
        class DesktopFactory(VNCDoToolFactory):
            qemu_extended_key = False
        host, port = target['display_host'], target['display_port']
        if not host or not port: raise RuntimeError('Desktop is not connected')
        key = (target['id'], target['session_key'])
        self._prune_display(target['id'], key)
        client = self.displays.get(key)
        connected = client is not None
        if client is None:
            client = api.connect(f'{host}::{int(port)}', factory_class=DesktopFactory, timeout=15)
            # Pay the WayVNC placeholder-frame cost only once per boot/session.
            client.refreshScreen()
            client.pause(0.2)
            self.displays[key] = client
        op = action['action']
        try:
            if op == 'screenshot':
                with tempfile.TemporaryDirectory(prefix='mola-screen-') as temp:
                    path = Path(temp) / 'screen.png'
                    client.refreshScreen()
                    client.captureScreen(str(path))
                    captured = time.perf_counter_ns()
                    value = {'mime_type': 'image/png', 'image_base64': base64.b64encode(path.read_bytes()).decode()}
                    return value, {'display_ms': round((captured-started)/1_000_000, 3),
                                   'serialize_ms': round((time.perf_counter_ns()-captured)/1_000_000, 3),
                                   'display_reused': connected}
            if op in ('click', 'move'):
                client.mouseMove(action['x'], action['y'])
                if op == 'click': client.mousePress(action.get('button', 1))
            elif op == 'scroll':
                for _ in range(action.get('amount', 3)): client.mousePress(4 if action['direction'] == 'up' else 5)
            elif op == 'type':
                for character in action['text']: client.keyPress({'\n': 'enter', '\t': 'tab'}.get(character, character))
            elif op == 'key':
                aliases = {'escape': 'esc', 'backspace': 'bsp', 'pageup': 'pgup', 'pagedown': 'pgdn', 'insert': 'ins'}
                client.keyPress('-'.join(aliases.get(key, key) for key in action['key'].split('-')))
            else: raise ValueError('Unsupported action')
            return {'ok': True}, {'display_ms': round((time.perf_counter_ns()-started)/1_000_000, 3), 'display_reused': connected}
        except Exception as error:
            # Discard a broken warm connection so the next explicit request can
            # reconnect. Never replay an input action whose outcome is unknown.
            self.displays.pop(key, None)
            try: client.disconnect()
            except Exception: pass
            raise ActionError('transport_failed' if op == 'screenshot' else 'action_outcome_unknown') from error

    def run(self, payload):
        target, action = payload['target'], payload['action']
        return self._ssh(target, action) if action['action'] in ('exec','read_file','write_file') else self._display(target, action)

    def close(self):
        for client in self.displays.values():
            try: client.disconnect()
            except Exception: pass
        try:
            from vncdotool import api
            api.shutdown()
        except Exception: pass


def run(payload):
    """Backward-compatible one-shot dispatcher used by older integrations."""
    payload = dict(payload)
    target = dict(payload['target'])
    target.setdefault('session_key', target.get('boot_id', 'legacy-one-shot'))
    target.setdefault('control_dir', str(Path(tempfile.gettempdir()) / 'mola-automation-control'))
    payload['target'] = target
    worker = Worker()
    try:
        result, _timing = worker.run(payload)
        return result
    finally:
        worker.close()


def respond(worker, request):
    started = time.perf_counter_ns()
    try:
        result, spans = worker.run(request)
        return {'id': request.get('id'), 'ok': True, 'result': result,
                'timing': {**spans, 'worker_ms': round((time.perf_counter_ns()-started)/1_000_000, 3)}}
    except Exception as error:
        return {'id': request.get('id'), 'ok': False, 'code': getattr(error, 'code', 'automation_failed'),
                'error': 'Automation transport failed.',
                'timing': {'worker_ms': round((time.perf_counter_ns()-started)/1_000_000, 3)}}


def main():
    os.umask(0o077)
    worker = Worker()
    try:
        if '--worker' in sys.argv:
            for line in sys.stdin:
                try: request = json.loads(line)
                except Exception:
                    print(json.dumps({'id': None, 'ok': False, 'code': 'invalid_request', 'error': 'Invalid worker request.'}), flush=True)
                    continue
                print(json.dumps(respond(worker, request), separators=(',', ':')), flush=True)
        else:
            request = json.load(sys.stdin)
            message = respond(worker, request)
            if not message['ok']:
                print(message['error'], file=sys.stderr)
                return 1
            print(json.dumps(message['result']))
    finally:
        worker.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
