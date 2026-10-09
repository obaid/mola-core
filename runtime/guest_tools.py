"""Guest-local tools. Source and private request arrive on SSH stdin only.

No request logging, arbitrary browser endpoint, shared customer profile, or
unchecked installer output. Raw disk checkpoints are a separate native feature.
"""
import base64
import ctypes
import ctypes.util
import hashlib
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zlib


class ToolError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


def require(condition, code='invalid_tool_arguments', status=400):
    if not condition:
        raise ToolError(code, status)


def state_dir():
    result = Path.home() / '.local/state/mola'
    result.mkdir(parents=True, exist_ok=True, mode=0o700)
    return result


def save(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.new')
    with open(temporary, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def read(path, fallback=None):
    return json.loads(path.read_text()) if path.exists() else fallback


def bounded_text(value, limit=8192):
    require(isinstance(value, str) and len(value) <= limit and '\0' not in value)
    return value


def web_url(value, https=False):
    bounded_text(value)
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme in (['https'] if https else ['https', 'http']) and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.fragment, 'invalid_browser_url')
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))


def command(args, timeout=10, binary=False):
    try:
        value = subprocess.run(args, capture_output=True, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError):
        raise ToolError('guest_dependency_unavailable', 501) from None
    require(len(value.stdout) <= 16 * 1024 * 1024, 'tool_response_too_large', 422)
    return value.stdout if binary else value.stdout.decode('utf8', 'replace')


def desktop_env():
    os.environ.setdefault('DISPLAY', ':1')
    os.environ.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
    bus = Path(os.environ['XDG_RUNTIME_DIR']) / 'bus'
    if bus.exists(): os.environ.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + str(bus))


class CDP:
    """Bounded RFC6455 client for a verified local browser endpoint."""
    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'ws' and parsed.hostname == '127.0.0.1' and parsed.port, 'invalid_browser_endpoint')
        self.socket = socket.create_connection(('127.0.0.1', parsed.port), timeout=15)
        self.socket.settimeout(15)
        self.buffer, self.next_id = b'', 0
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        self.socket.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: 127.0.0.1:' + str(parsed.port)
                             + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ' + key
                             + '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            chunk = self.socket.recv(4096); require(bool(chunk), 'browser_disconnected', 503)
            self.buffer += chunk
            require(len(self.buffer) < 65536, 'invalid_browser_endpoint')
        header, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        require(header.startswith(b'HTTP/1.1 101') and accept in header, 'browser_connection_failed', 503)

    def close(self): self.socket.close()

    def exact(self, size):
        require(size <= 24 * 1024 * 1024, 'tool_response_too_large', 422)
        while len(self.buffer) < size:
            chunk = self.socket.recv(min(65536, size - len(self.buffer)))
            require(bool(chunk), 'browser_disconnected', 503)
            self.buffer += chunk
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def send(self, data, opcode=1):
        data = data.encode() if isinstance(data, str) else data
        mask = os.urandom(4); size = len(data)
        header = bytes([128 | opcode, 128 | (size if size < 126 else 126 if size < 65536 else 127)])
        if size >= 126: header += struct.pack('!H' if size < 65536 else '!Q', size)
        self.socket.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def receive(self):
        fragments = b''
        while True:
            first, second = self.exact(2); opcode = first & 15; size = second & 127
            if size == 126: size = struct.unpack('!H', self.exact(2))[0]
            elif size == 127: size = struct.unpack('!Q', self.exact(8))[0]
            mask = self.exact(4) if second & 128 else None
            data = self.exact(size)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8: raise ToolError('browser_disconnected', 503)
            if opcode == 9: self.send(data, 10); continue
            if opcode == 10: continue
            fragments += data
            require(len(fragments) <= 24 * 1024 * 1024, 'tool_response_too_large', 422)
            if first & 128: return json.loads(fragments)

    def call(self, method, params=None):
        self.next_id += 1
        self.send(json.dumps({'id': self.next_id, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = self.receive()
            if value.get('id') != self.next_id: continue
            require('error' not in value, 'browser_command_failed', 422)
            return value.get('result', {})
        raise ToolError('browser_timeout_outcome_unknown', 503)

    def evaluate(self, expression):
        value = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True, 'timeout': 10000})
        require('exceptionDetails' not in value, 'browser_evaluation_failed', 422)
        return value.get('result', {}).get('value')


def profile_process(pid, profile):
    proc = Path('/proc') / str(pid)
    if not proc.exists(): return False
    try:
        executable = Path(os.readlink(proc / 'exe')).name
        raw = (proc / 'cmdline').read_bytes()
        # Chromium rewrites Linux argv into a single process-title string.
        # NUL-list membership therefore cannot establish the launch profile.
        return executable in ['chrome', 'chromium', 'chromium-browser', 'google-chrome'] and bool(re.search(rb'(?:^|\x00|\s)--user-data-dir=' + re.escape(str(profile).encode()) + rb'(?=\x00|\s|$)', raw))
    except (FileNotFoundError, PermissionError): return False


def chrome_processes():
    for candidate in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            executable = Path(os.readlink(candidate.parent / 'exe')).name
            raw = candidate.read_bytes()
            if candidate.stat().st_uid == os.getuid() and executable in ['chrome', 'chromium', 'chromium-browser', 'google-chrome'] and not re.search(rb'(?:^|\x00|\s)--type=', raw):
                yield int(candidate.parent.name), raw
        except (FileNotFoundError, PermissionError): continue


def wait_profile_closed(profile):
    for _ in range(200):
        holders = []
        for candidate in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                if candidate.stat().st_uid == os.getuid() and profile_process(int(candidate.parent.name), profile): holders.append(candidate.parent.name)
            except (FileNotFoundError, PermissionError): continue
        if not holders: return
        time.sleep(.05)
    raise ToolError('browser_shutdown_outcome_unknown', 503)


def browser_state():
    state = read(state_dir() / 'browser.json')
    require(state is not None, 'browser_prepare_required')
    profile = Path(state['profile'])
    require(profile == Path.home() / '.local/share/mola/browser/profile', 'invalid_browser_profile')
    require(profile_process(state['pid'], profile), 'browser_not_running')
    endpoint = (profile / 'DevToolsActivePort').read_text().splitlines()
    port = int(endpoint[0]); require(1024 <= port <= 65535, 'invalid_browser_endpoint')
    return state, port


def targets():
    state, port = browser_state()
    with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/json/list', timeout=5) as response:
        raw = response.read(1024 * 1024 + 1)
    require(len(raw) <= 1024 * 1024, 'tool_response_too_large', 422)
    pages = [p for p in json.loads(raw) if p.get('type') == 'page']
    return state, pages


def page(arguments):
    state, pages = targets()
    selected = arguments.get('tab_id') or state.get('current_tab')
    matches = [p for p in pages if p['id'] == selected] if selected else []
    if not selected:
        prior = [p for p in pages if p['url'] == state.get('last_current_url')]
        matches = prior if len(prior) == 1 else pages[:1]
    require(len(matches) == 1, 'browser_tab_not_found')
    return state, matches[0], CDP(matches[0]['webSocketDebuggerUrl'])


def prepare(arguments):
    mode = arguments.get('mode', 'full'); require(mode in ['full', 'lightweight'])
    desktop_env()
    root = Path.home() / '.local/share/mola/browser'; root.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = root / 'profile'
    original = Path.home() / '.config/google-chrome'
    # Never attach a second browser to a live SQLite profile or kill a customer's
    # existing browser implicitly. The caller must close it before migration.
    previous = read(state_dir() / 'browser.json', {})
    for pid, raw in list(chrome_processes()):
        if profile_process(pid, profile):
            if previous.get('mode') == mode and previous.get('software_webgl') is True:
                browser_state(); return {'prepared': True, 'mode': mode, 'profile': 'managed', 'migration': 'retained'}
            require(arguments.get('allow_restart') is True, 'browser_mode_restart_consent_required')
            os.kill(pid, signal.SIGTERM)
            wait_profile_closed(profile)
            backup = root / ('mode-backup-' + uuid.uuid4().hex)
            shutil.copytree(profile, backup, ignore=shutil.ignore_patterns('Singleton*', 'DevToolsActivePort', 'Crash Reports', 'BrowserMetrics*', '*.pma'))
        else: raise ToolError('existing_browser_must_close_before_prepare', 503)
    require(not profile.is_symlink() and not original.is_symlink(), 'unsafe_browser_profile')
    if not profile.exists():
        stage = root / ('.profile-' + uuid.uuid4().hex)
        if original.exists():
            shutil.copytree(original, stage, symlinks=False, ignore=shutil.ignore_patterns('Singleton*', 'DevToolsActivePort'))
        else: stage.mkdir(mode=0o700)
        stage.rename(profile)
    executable = next((p for p in ['/opt/google/chrome/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'] if Path(p).is_file()), None)
    require(executable is not None, 'full_browser_unavailable', 501)
    (profile / 'DevToolsActivePort').unlink(missing_ok=True)
    # CPU-only guest desktops deliberately select Chrome's software WebGL
    # renderer. Keep Chrome's sandbox; never use --no-sandbox for this feature.
    flags = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']
    if mode == 'lightweight':
        try: dimensions = screen()
        except ToolError: dimensions = {'width': 1440, 'height': 900}
        flags += ['--headless=new', '--window-size=' + str(dimensions['width']) + ',' + str(dimensions['height'])]
    child = subprocess.Popen([executable, *flags, '--user-data-dir=' + str(profile), '--profile-directory=Default', '--no-first-run',
                              '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                              '--restore-last-session'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    save(state_dir() / 'browser.json', {'profile': str(profile), 'pid': child.pid, 'mode': mode, 'software_webgl': True, 'current_tab': None, 'last_current_url': previous.get('last_current_url')})
    for _ in range(60):
        if (profile / 'DevToolsActivePort').exists():
            browser_state(); return {'prepared': True, 'mode': mode, 'profile': 'managed', 'migration': 'source_preserved'}
        require(child.poll() is None, 'browser_launch_failed', 503); time.sleep(0.1)
    raise ToolError('browser_starting', 503)


SNAPSHOT = r'''(() => { const token=crypto.randomUUID?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');window.__molaSnapshot=token;
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
const elements=[...document.querySelectorAll('a,button,input,textarea,select,[role],[contenteditable="true"]')].filter(visible).slice(0,500);
return {url:location.href,title:document.title,snapshot_id:token,viewport:{width:innerWidth,height:innerHeight},
text:(document.body?.innerText||'').slice(0,64000),ready_state:document.readyState,elements:elements.map((e,i)=>{e.dataset.molaRef=String(i);const r=e.getBoundingClientRect();return {ref:String(i),tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),name:(e.getAttribute('aria-label')||e.innerText||e.getAttribute('placeholder')||'').slice(0,1000),type:e.type||null,disabled:!!e.disabled,bounds:{x:r.x,y:r.y,width:r.width,height:r.height}}})};})()'''


def element_expression(arguments, operation):
    ref = bounded_text(arguments.get('ref'), 10); require(ref.isdigit())
    snapshot = bounded_text(arguments.get('snapshot_id'), 64)
    return "(() => {if(window.__molaSnapshot!==" + json.dumps(snapshot) + ")throw Error('stale');const e=document.querySelector('[data-mola-ref=\"" + ref + "\"]');if(!e||e.disabled)throw Error('missing');" + operation + ';return {success:true};})()'


def browser(tool, arguments):
    if tool == 'browser_prepare': return prepare(arguments)
    if tool == 'browser_capabilities':
        return {'engine': 'installed-chrome-cdp', 'full': Path('/opt/google/chrome/google-chrome').exists(), 'lightweight': Path('/opt/google/chrome/google-chrome').exists(),
                'lightweight_engine': 'same Chrome binary with headless=new; UI mode', 'lightweight_qualification': 'image acceptance required', 'tools': ['navigate', 'snapshot', 'click', 'fill', 'key', 'evaluate', 'screenshot', 'tabs'],
                'profile_migration': 'offline_source_preserved', 'signed_in_migration_qualified': False}
    if tool == 'browser_tabs':
        state, pages = targets(); action = arguments.get('action', 'list')
        if action == 'select':
            wanted = arguments.get('tab_id'); require(any(p['id'] == wanted for p in pages), 'browser_tab_not_found')
            state['current_tab'] = wanted; save(state_dir() / 'browser.json', state)
            _, _, cdp = page({'tab_id': wanted})
            try: cdp.call('Page.bringToFront')
            finally: cdp.close()
        elif action in ('new', 'close'):
            state, port = browser_state(); endpoint = (Path(state['profile']) / 'DevToolsActivePort').read_text().splitlines()[1]
            cdp = CDP('ws://127.0.0.1:' + str(port) + endpoint)
            try:
                if action == 'new':
                    result = cdp.call('Target.createTarget', {'url': web_url(arguments.get('url', 'https://example.com/'))}); state['current_tab'] = result['targetId']; save(state_dir() / 'browser.json', state)
                else:
                    require(any(p['id'] == arguments.get('tab_id') for p in pages), 'browser_tab_not_found')
                    cdp.call('Target.closeTarget', {'targetId': arguments['tab_id']})
            finally: cdp.close()
        else: require(action == 'list')
        state, pages = targets()
        return {'current_tab': state.get('current_tab'), 'tabs': [{'id': p['id'], 'url': p['url'], 'title': p['title']} for p in pages]}
    state, target, cdp = page(arguments)
    try:
        if tool == 'browser_navigate':
            result = cdp.call('Page.navigate', {'url': web_url(arguments.get('url'))}); require(not result.get('errorText'), 'browser_navigation_failed', 422)
            for _ in range(40):
                if cdp.evaluate('document.readyState') in ['interactive', 'complete']: break
                time.sleep(0.1)
            actual = cdp.evaluate('location.href'); state['current_tab'] = target['id']; state['last_current_url'] = actual; save(state_dir() / 'browser.json', state)
            return {'tab_id': target['id'], 'url': actual}
        if tool == 'browser_snapshot': return {'tab_id': target['id'], **cdp.evaluate(SNAPSHOT)}
        if tool == 'browser_click': return cdp.evaluate(element_expression(arguments, 'e.scrollIntoView({block:"center"});e.click()'))
        if tool == 'browser_fill':
            value = bounded_text(arguments.get('value'), 65536)
            return cdp.evaluate(element_expression(arguments, 'if(!["INPUT","TEXTAREA"].includes(e.tagName)||e.type==="password")throw Error("use vault");Object.getOwnPropertyDescriptor(e.tagName==="INPUT"?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,"value").set.call(e,' + json.dumps(value) + ');e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new Event("change",{bubbles:true}))'))
        if tool == 'browser_evaluate': return {'value': cdp.evaluate(bounded_text(arguments.get('expression'), 65536))}
        if tool == 'browser_key':
            key = bounded_text(arguments.get('key'), 32); require(key in ['Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'])
            for event in ['keyDown', 'keyUp']: cdp.call('Input.dispatchKeyEvent', {'type': event, 'key': key})
            return {'success': True}
        if tool == 'browser_screenshot': return browser_capture(cdp, {**arguments, '_tab_id': target['id']})
        raise ToolError('unsupported_computer_tool', 400)
    finally: cdp.close()


def crop_box(crop, width, height):
    if crop is None: return {'x': 0, 'y': 0, 'width': width, 'height': height}
    require(isinstance(crop, dict) and set(crop) == {'x', 'y', 'width', 'height'})
    require(all(type(v) is int for v in crop.values()) and crop['x'] >= 0 and crop['y'] >= 0 and crop['width'] > 0 and crop['height'] > 0)
    require(crop['x'] + crop['width'] <= width and crop['y'] + crop['height'] <= height, 'crop_outside_target')
    return crop


def browser_area(cdp, arguments):
    metadata = cdp.evaluate('({url:location.href,title:document.title,width:innerWidth,height:innerHeight})')
    allowed = arguments.get('allowed_origin')
    if allowed is not None:
        # Viewer policy binds an origin; SPA paths, queries and fragments may
        # change within it. Vault injection separately binds the exact URL.
        origin = urllib.parse.urlsplit(metadata['url']); policy = urllib.parse.urlsplit(web_url(allowed, https=True))
        require(origin.scheme == 'https' and origin.hostname and origin.username is None and origin.password is None
                and policy.path in ['', '/'] and not policy.query
                and origin.hostname.lower() == policy.hostname.lower() and (origin.port or 443) == (policy.port or 443), 'browser_view_origin_mismatch')
    bounds = {'x':0,'y':0,**{k:metadata[k] for k in ['width', 'height']}}
    selector = arguments.get('selector')
    if selector is not None:
        require(allowed is not None,'selector_origin_required')
        bounded_text(selector,512); require(selector and not any(ord(c)<32 for c in selector), 'invalid_view_selector')
        rectangle=cdp.evaluate('(()=>{const es=document.querySelectorAll('+json.dumps(selector)+');if(es.length!==1)return null;const e=es[0],r=e.getBoundingClientRect(),s=getComputedStyle(e);if(s.visibility!=="visible"||s.display==="none"||r.width<=0||r.height<=0)return null;const x=Math.max(0,Math.floor(r.left)),y=Math.max(0,Math.floor(r.top));return {x,y,width:Math.min(innerWidth,Math.ceil(r.right))-x,height:Math.min(innerHeight,Math.ceil(r.bottom))-y};})()')
        require(isinstance(rectangle,dict) and rectangle.get('width',0)>0 and rectangle.get('height',0)>0, 'browser_view_selector_unavailable',409)
        bounds=rectangle
    relative = crop_box(arguments.get('crop'), bounds['width'], bounds['height'])
    crop = {**relative,'x':bounds['x']+relative['x'],'y':bounds['y']+relative['y']}
    return metadata,bounds,crop


def browser_capture(cdp, arguments):
    frame = cdp.call('Page.getFrameTree')['frameTree']['frame']
    metadata,bounds,crop=browser_area(cdp,arguments)
    require(metadata['url'] == frame['url'], 'browser_capture_navigation_changed', 503)
    image = cdp.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False,
                                              'clip': {**crop, 'scale': 1}, 'fromSurface': True})['data']
    after = cdp.call('Page.getFrameTree')['frameTree']['frame']
    require(after.get('loaderId') == frame.get('loaderId') and after['url'] == frame['url'], 'browser_capture_navigation_changed', 503)
    if arguments.get('selector') is not None:
        after_metadata,after_bounds,_=browser_area(cdp,arguments)
        require(after_bounds==bounds and after_metadata['width']==metadata['width'] and after_metadata['height']==metadata['height'], 'browser_view_geometry_changed',409)
    return {'mime_type': 'image/png', 'image_base64': image, 'geometry': crop, 'target_geometry': bounds,
            'url': metadata['url'], 'title': metadata['title'], 'tab_id': arguments.get('_tab_id')}


def windows():
    value = json.loads(command(['/usr/local/bin/mola-tile', 'list']))
    return value['windows']


class WindowConnection:
    """XRes 1.2 local-client PID proof; never trust _NET_WM_PID.

    Scoped capture/events use this same connection while XGrabServer prevents
    another client destroying/reallocating the XID between proof and use.
    """
    class Spec(ctypes.Structure):
        _fields_ = [('client', ctypes.c_ulong), ('mask', ctypes.c_uint)]
    class Image(ctypes.Structure):
        _fields_ = [('width', ctypes.c_int), ('height', ctypes.c_int), ('xoffset', ctypes.c_int),
                   ('format', ctypes.c_int), ('data', ctypes.c_void_p), ('byte_order', ctypes.c_int),
                   ('bitmap_unit', ctypes.c_int), ('bitmap_bit_order', ctypes.c_int), ('bitmap_pad', ctypes.c_int),
                   ('depth', ctypes.c_int), ('bytes_per_line', ctypes.c_int), ('bits_per_pixel', ctypes.c_int),
                   ('red_mask', ctypes.c_ulong), ('green_mask', ctypes.c_ulong), ('blue_mask', ctypes.c_ulong)]
    class ClassHint(ctypes.Structure):
        _fields_ = [('name', ctypes.c_void_p), ('klass', ctypes.c_void_p)]
    class XError(ctypes.Structure):
        _fields_ = [('type',ctypes.c_int),('display',ctypes.c_void_p),('resource',ctypes.c_ulong),
                   ('serial',ctypes.c_ulong),('code',ctypes.c_ubyte),('request',ctypes.c_ubyte),('minor',ctypes.c_ubyte)]
    class Event(ctypes.Structure):
        _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                   ('display', ctypes.c_void_p), ('window', ctypes.c_ulong), ('root', ctypes.c_ulong),
                   ('subwindow', ctypes.c_ulong), ('time', ctypes.c_ulong), ('x', ctypes.c_int), ('y', ctypes.c_int),
                   ('x_root', ctypes.c_int), ('y_root', ctypes.c_int), ('state', ctypes.c_uint),
                   ('button', ctypes.c_uint), ('same_screen', ctypes.c_int), ('padding', ctypes.c_byte * 128)]

    def __init__(self, window_id):
        require(isinstance(window_id, str) and re.fullmatch(r'0x[0-9a-fA-F]{1,8}', window_id) and int(window_id,16)>0, 'invalid_window_id')
        self.window = int(window_id,16); self.display = None; self.grabbed = False
        try:
            self.x = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
            self.res = ctypes.CDLL(ctypes.util.find_library('XRes') or 'libXRes.so.1')
            class Value(ctypes.Structure):
                _fields_ = [('spec', WindowConnection.Spec), ('length', ctypes.c_long), ('value', ctypes.c_void_p)]
            self.Value = Value
            def bind(library, name, result, arguments):
                method = getattr(library,name); method.restype=result; method.argtypes=arguments
            ptr, integer, xid = ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong
            bind(self.x,'XOpenDisplay',ptr,[ctypes.c_char_p]); bind(self.x,'XCloseDisplay',integer,[ptr])
            bind(self.x,'XSync',integer,[ptr,integer]); bind(self.x,'XGrabServer',integer,[ptr]); bind(self.x,'XUngrabServer',integer,[ptr])
            bind(self.x,'XSetErrorHandler',ptr,[ptr]); bind(self.x,'XFree',integer,[ptr])
            bind(self.x,'XInternAtom',xid,[ptr,ctypes.c_char_p,integer])
            bind(self.x,'XGetWindowProperty',integer,[ptr,xid,xid,ctypes.c_long,ctypes.c_long,integer,xid,ctypes.POINTER(xid),ctypes.POINTER(integer),ctypes.POINTER(xid),ctypes.POINTER(xid),ctypes.POINTER(ptr)])
            bind(self.x,'XChangeProperty',integer,[ptr,xid,xid,xid,integer,integer,ptr,integer])
            bind(self.x,'XGetClassHint',integer,[ptr,xid,ctypes.POINTER(self.ClassHint)])
            bind(self.x,'XDefaultRootWindow',xid,[ptr])
            bind(self.x,'XQueryExtension',integer,[ptr,ctypes.c_char_p,*[ctypes.POINTER(integer)]*3])
            bind(self.x,'XQueryTree',integer,[ptr,xid,ctypes.POINTER(xid),ctypes.POINTER(xid),ctypes.POINTER(ctypes.POINTER(xid)),ctypes.POINTER(ctypes.c_uint)])
            bind(self.x,'XTranslateCoordinates',integer,[ptr,xid,xid,integer,integer,ctypes.POINTER(integer),ctypes.POINTER(integer),ctypes.POINTER(xid)])
            bind(self.x,'XFreePixmap',integer,[ptr,xid])
            bind(self.x,'XGetGeometry',integer,[ptr,xid,ctypes.POINTER(xid),*[ctypes.POINTER(integer)]*2,*[ctypes.POINTER(ctypes.c_uint)]*4])
            bind(self.x,'XGetImage',ctypes.POINTER(self.Image),[ptr,xid,integer,integer,ctypes.c_uint,ctypes.c_uint,xid,integer])
            bind(self.x,'XDestroyImage',integer,[ctypes.POINTER(self.Image)])
            bind(self.x,'XSendEvent',integer,[ptr,xid,integer,ctypes.c_long,ptr])
            bind(self.res,'XResQueryVersion',integer,[ptr,ctypes.POINTER(integer),ctypes.POINTER(integer)])
            bind(self.res,'XResQueryClientIds',integer,[ptr,ctypes.c_long,ctypes.POINTER(self.Spec),ctypes.POINTER(ctypes.c_long),ctypes.POINTER(ctypes.POINTER(Value))])
            bind(self.res,'XResGetClientPid',integer,[ctypes.POINTER(Value)])
            bind(self.res,'XResClientIdsDestroy',None,[ctypes.c_long,ctypes.POINTER(Value)])
            self.errors = False
            self.failures=[]
            self.handler = ctypes.CFUNCTYPE(integer,ptr,ptr)(lambda _,error: self._error(error))
            self.previous_handler = self.x.XSetErrorHandler(ctypes.cast(self.handler,ptr))
            self.display=self.x.XOpenDisplay(None)
            require(bool(self.display),'window_identity_unavailable',501)
            major,minor=integer(),integer()
            require(self.res.XResQueryVersion(self.display,ctypes.byref(major),ctypes.byref(minor)) and (major.value,minor.value)>=(1,2),'window_identity_unavailable',501)
        except (OSError,AttributeError):
            self.close(); raise ToolError('window_identity_unavailable',501) from None
        except Exception:
            self.close(); raise

    def _error(self,error):
        self.errors=True;value=ctypes.cast(error,ctypes.POINTER(self.XError)).contents
        self.failures.append((value.code,value.request,value.minor));return 0
    def __enter__(self):
        self.x.XGrabServer(self.display); self.x.XSync(self.display,0); self.grabbed=True; return self
    def __exit__(self,*_): self.close()
    def close(self):
        if self.display:
            if self.grabbed:self.x.XUngrabServer(self.display)
            self.x.XSync(self.display,0); self.x.XCloseDisplay(self.display); self.display=None
        if hasattr(self,'previous_handler'):self.x.XSetErrorHandler(self.previous_handler); del self.previous_handler
    def instance(self, create=False):
        atom=self.x.XInternAtom(self.display,b'_MOLA_VIEWER_INSTANCE',0)
        kind=ctypes.c_ulong(); format=ctypes.c_int(); count=ctypes.c_ulong(); remaining=ctypes.c_ulong(); data=ctypes.c_void_p()
        try:
            require(self.x.XGetWindowProperty(self.display,self.window,atom,0,17,0,31,ctypes.byref(kind),ctypes.byref(format),ctypes.byref(count),ctypes.byref(remaining),ctypes.byref(data))==0,'window_identity_changed')
            self.x.XSync(self.display,0);require(not self.errors,'window_identity_changed')
            if kind.value==0:
                if not create:return None
                nonce=os.urandom(32).hex();buffer=ctypes.create_string_buffer(nonce.encode())
                self.x.XChangeProperty(self.display,self.window,atom,31,8,0,buffer,64)
                self.x.XSync(self.display,0);require(not self.errors,'window_identity_changed')
                require(self.instance(False)==nonce,'window_identity_changed');return nonce
            require(kind.value==31 and format.value==8 and count.value==64 and remaining.value==0 and bool(data),'window_identity_changed')
            nonce=ctypes.string_at(data,64).decode('ascii','strict')
            require(re.fullmatch(r'[0-9a-f]{64}',nonce),'window_identity_changed');return nonce
        except UnicodeError:raise ToolError('window_identity_changed') from None
        finally:
            if data:self.x.XFree(data)
    def identity(self, create_instance=False, require_instance=True):
        count=ctypes.c_long(); values=ctypes.POINTER(self.Value)(); spec=self.Spec(self.window,2)
        pid=None
        try:
            require(self.res.XResQueryClientIds(self.display,1,ctypes.byref(spec),ctypes.byref(count),ctypes.byref(values))==0,'window_identity_unavailable',501)
            require(0<count.value<=16,'window_identity_unavailable',501)
            for index in range(count.value):
                candidate=self.res.XResGetClientPid(ctypes.byref(values[index]))
                if candidate>0:pid=candidate; break
        finally:
            if values:self.res.XResClientIdsDestroy(count,values)
        require(pid is not None,'window_identity_unavailable',501)
        hint=self.ClassHint(); klass=[]
        try:
            require(self.x.XGetClassHint(self.display,self.window,ctypes.byref(hint))!=0,'window_identity_unavailable',501)
            for field in [hint.name,hint.klass]:
                require(bool(field),'window_identity_unavailable',501)
                value=ctypes.string_at(field)
                require(len(value)<=256,'window_identity_unavailable',501); klass.append(value.decode('utf8','strict'))
        finally:
            for field in [hint.name,hint.klass]:
                if field:self.x.XFree(field)
        try:
            stat=(Path('/proc')/str(pid)/'stat').read_text(); tail=stat.rsplit(')',1)[1].split()
            require(tail[0] not in ['Z','X'] and re.fullmatch(r'[1-9][0-9]{0,19}',tail[19]),'window_identity_unavailable',501)
            boot=str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
        except (OSError,ValueError,IndexError,UnicodeError):raise ToolError('window_identity_unavailable',501) from None
        self.x.XSync(self.display,0); require(not self.errors,'window_identity_changed')
        identity={'window_id':hex(self.window),'pid':pid,'start_ticks':tail[19],'boot_id':boot,'wm_class':klass}
        nonce=self.instance(create_instance)
        require(nonce is not None or not require_instance,'window_identity_changed')
        if nonce is not None:identity['instance_nonce']=nonce
        return identity
    def verify(self, expected):
        require(isinstance(expected,dict) and set(expected)=={'window_id','pid','start_ticks','boot_id','wm_class','instance_nonce'},'window_identity_required')
        require(self.identity()==expected,'window_identity_changed')
    def drawable_geometry(self, drawable):
        root=ctypes.c_ulong(); x,y=ctypes.c_int(),ctypes.c_int(); w,h,b,d=(ctypes.c_uint() for _ in range(4))
        require(self.x.XGetGeometry(self.display,drawable,ctypes.byref(root),ctypes.byref(x),ctypes.byref(y),ctypes.byref(w),ctypes.byref(h),ctypes.byref(b),ctypes.byref(d))!=0,'window_not_found')
        self.x.XSync(self.display,0); require(not self.errors and 0<w.value<=4096 and 0<h.value<=2400 and b.value<=128,'window_geometry_unavailable')
        return w.value,h.value,b.value
    def bounds(self):
        w,h,self.border=self.drawable_geometry(self.window)
        require(w<=3840 and h<=2160,'window_geometry_unavailable')
        self.client_dimensions=(w,h)
        return {'width':w,'height':h}
    def offscreen_pixmap(self):
        # XGetImage(window) has undefined pixels where another window obscures
        # it. Only an existing compositor's verified offscreen pixmap is safe.
        # Never start redirection here: its initial hidden pixels may be stale.
        try:
            composite=ctypes.CDLL(ctypes.util.find_library('Xcomposite') or 'libXcomposite.so.1')
            composite.XCompositeQueryVersion.restype=ctypes.c_int
            composite.XCompositeQueryVersion.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_int)]
            composite.XCompositeNameWindowPixmap.restype=ctypes.c_ulong
            composite.XCompositeNameWindowPixmap.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
            major,minor=ctypes.c_int(),ctypes.c_int()
            require(composite.XCompositeQueryVersion(self.display,ctypes.byref(major),ctypes.byref(minor)) and (major.value,minor.value)>=(0,2),'window_capture_unsupported',501)
            opcode,event,error=ctypes.c_int(),ctypes.c_int(),ctypes.c_int()
            require(self.x.XQueryExtension(self.display,b'Composite',ctypes.byref(opcode),ctypes.byref(event),ctypes.byref(error))!=0,'window_capture_unsupported',501)
        except (OSError,AttributeError):raise ToolError('window_capture_unsupported',501) from None
        path=[self.window];root=self.x.XDefaultRootWindow(self.display)
        require(self.window!=root,'window_capture_unsupported',501)
        for _ in range(8):
            candidate=path[-1]
            if candidate==self.window:w,h=self.client_dimensions;border=self.border;offset=(border,border)
            else:
                w,h,border=self.drawable_geometry(candidate)
                self.prove_client_region(path)
                x,y=self.translate(self.window,candidate);offset=(x+border,y+border)
            pixmap=self.named_pixmap(composite,opcode.value,candidate,w,h,border)
            if pixmap is not None:
                self.capture_offset=offset;return pixmap
            _,parent,_=self.tree(candidate)
            require(parent not in [0,root] and parent not in path,'window_capture_unsupported',501)
            path.append(parent)
        raise ToolError('window_capture_unsupported',501)
    def named_pixmap(self, composite, opcode, window, width, height, border):
        require(not self.errors,'window_identity_changed')
        start=len(getattr(self,'failures',[]))
        pixmap=composite.XCompositeNameWindowPixmap(self.display,window)
        self.x.XSync(self.display,0)
        if self.errors:
            # Only an expected unredirected-window BadMatch can advance. Other
            # errors, including destroyed/reparented resources, are terminal.
            if getattr(self,'failures',[])[start:]==[(8,opcode,6)]:
                self.errors=False;return None
            raise ToolError('window_capture_unsupported',501)
        require(pixmap,'window_capture_unsupported',501)
        try:
            w,h,pixmap_border=self.drawable_geometry(pixmap)
            require(pixmap_border==0 and (w,h)==(width+2*border,height+2*border),'window_capture_unsupported',501)
            return pixmap
        except Exception:
            self.x.XFreePixmap(self.display,pixmap);raise
    def tree(self,window):
        root,parent=ctypes.c_ulong(),ctypes.c_ulong();children=ctypes.POINTER(ctypes.c_ulong)();count=ctypes.c_uint()
        try:
            require(self.x.XQueryTree(self.display,window,ctypes.byref(root),ctypes.byref(parent),ctypes.byref(children),ctypes.byref(count))!=0,'window_capture_unsupported',501)
            self.x.XSync(self.display,0)
            require(not self.errors and root.value==self.x.XDefaultRootWindow(self.display) and count.value<=256,'window_capture_unsupported',501)
            return root.value,parent.value,[children[i] for i in range(count.value)]
        finally:
            if children:self.x.XFree(children)
    def translate(self,source,target):
        x,y=ctypes.c_int(),ctypes.c_int();child=ctypes.c_ulong()
        require(self.x.XTranslateCoordinates(self.display,source,target,0,0,ctypes.byref(x),ctypes.byref(y),ctypes.byref(child))!=0,'window_capture_unsupported',501)
        self.x.XSync(self.display,0);require(not self.errors,'window_identity_changed')
        return x.value,y.value
    def prove_client_region(self,path):
        width,height=self.client_dimensions
        for index in range(1,len(path)):
            child,parent=path[index-1],path[index]
            _,_,siblings=self.tree(parent)
            require(child in siblings,'window_identity_changed')
            x,y=self.translate(self.window,parent);w,h,_=self.drawable_geometry(parent)
            require(x>=0 and y>=0 and x+width<=w and y+height<=h,'window_capture_unsupported',501)
            for sibling in siblings:
                if sibling==child:continue
                sw,sh,border=self.drawable_geometry(sibling);sx,sy=self.translate(sibling,self.window)
                # Even an unmapped sibling is conservatively rejected. No
                # non-path window can contribute pixels inside this crop.
                require(not (sx-border<width and sx+sw+border>0 and sy-border<height and sy+sh+border>0),'window_capture_unsupported',501)
    def probe_offscreen(self):
        self.bounds();pixmap=self.offscreen_pixmap()
        self.x.XFreePixmap(self.display,pixmap)
        self.x.XSync(self.display,0);require(not self.errors,'window_capture_unsupported',501)
    def pixels(self,crop):
        pixmap=self.offscreen_pixmap();image=None
        try:
            image=self.x.XGetImage(self.display,pixmap,self.capture_offset[0]+crop['x'],self.capture_offset[1]+crop['y'],crop['width'],crop['height'],ctypes.c_ulong(-1).value,2)
            self.x.XSync(self.display,0);require(bool(image) and not self.errors,'window_capture_unavailable')
            value=image.contents
            require(value.bits_per_pixel in [16,24,32] and value.byte_order in [0,1] and value.width*value.bits_per_pixel//8<=value.bytes_per_line<=65536 and value.height==crop['height'] and value.width==crop['width'],'window_capture_unsupported',501)
            raw=ctypes.string_at(value.data,value.bytes_per_line*value.height)
            return raw,value.bytes_per_line,value.bits_per_pixel,value.byte_order,[value.red_mask,value.green_mask,value.blue_mask]
        finally:
            if image:self.x.XDestroyImage(image)
            self.x.XFreePixmap(self.display,pixmap)
    def event(self, kind, x, y, button=0, state=0):
        event=self.Event(); event.type=kind; event.display=self.display; event.window=self.window
        event.root=self.x.XDefaultRootWindow(self.display); event.x=x;event.y=y;event.same_screen=1;event.button=button;event.state=state
        require(self.x.XSendEvent(self.display,self.window,0,{4:4,5:8,6:64}[kind],ctypes.byref(event))!=0,'window_input_rejected')
        self.x.XSync(self.display,0);require(not self.errors,'window_identity_changed')


def window_identity(arguments):
    desktop_env()
    with WindowConnection(arguments.get('window_id')) as connection:
        connection.identity(require_instance=False)
        connection.probe_offscreen()
        return connection.identity(create_instance=True)


def window_prepare(arguments):
    # Changing the desktop compositor is an explicit account-authorized setup
    # action. Capture/identity never silently change the customer's settings.
    require(arguments.get('allow_compositor') is True,'compositor_consent_required')
    desktop_env()
    with WindowConnection(arguments.get('window_id')) as connection:
        identity=connection.identity(require_instance=False)
    pids=command(['/usr/bin/pgrep','-u',str(os.getuid()),'-x','xfwm4'],timeout=5).split()
    require(len(pids)==1 and re.fullmatch(r'[1-9][0-9]{0,9}',pids[0]),'window_compositor_unsupported',501)
    try:require(os.readlink('/proc/'+pids[0]+'/exe')=='/usr/bin/xfwm4','window_compositor_unsupported',501)
    except OSError:raise ToolError('window_compositor_unsupported',501) from None
    setting=['/usr/bin/xfconf-query','-c','xfwm4','-p','/general/use_compositing']
    current=command(setting,timeout=5).strip()
    require(current in ['true','false'],'window_compositor_unsupported',501)
    if current=='false':command(setting+['-s','true'],timeout=5)
    deadline=time.monotonic()+8
    while True:
        try:
            with WindowConnection(arguments.get('window_id')) as connection:
                require(connection.identity(require_instance=False)==identity,'window_identity_changed')
                connection.probe_offscreen()
            return {'success':True}
        except ToolError as error:
            if error.code!='window_capture_unsupported' or time.monotonic()>=deadline:raise
            time.sleep(0.2)


def session_windows():
    result=[]
    for window in windows():
        try:
            with WindowConnection(window.get('id')) as connection:
                identity=connection.identity(require_instance=False)
            result.append({**window,'wm_class':identity['wm_class']})
        except ToolError:result.append(window)
    return result


def window_png(raw, stride, bits, order, masks, width, height):
    channels=[]
    for mask in masks:
        require(mask>0,'window_capture_unsupported',501)
        shift=(mask & -mask).bit_length()-1; maximum=mask>>shift
        require(maximum & (maximum+1)==0,'window_capture_unsupported',501);channels.append((mask,shift,maximum))
    data=bytearray(); size=bits//8
    for y in range(height):
        data.append(0)
        for x in range(width):
            offset=y*stride+x*size; pixel=int.from_bytes(raw[offset:offset+size],'little' if order==0 else 'big')
            data.extend(((pixel & mask)>>shift)*255//maximum for mask,shift,maximum in channels)
    def chunk(kind,payload):return struct.pack('!I',len(payload))+kind+payload+struct.pack('!I',zlib.crc32(kind+payload)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(data))+chunk(b'IEND',b'')


def scoped_window_capture(arguments):
    require(isinstance(arguments.get('window_identity'),dict) and set(arguments['window_identity'])=={'window_id','pid','start_ticks','boot_id','wm_class','instance_nonce'},'window_identity_required')
    with WindowConnection(arguments.get('window_id')) as connection:
        connection.verify(arguments.get('window_identity'));target=connection.bounds()
        crop=crop_box(arguments.get('crop'),**target);pixels=connection.pixels(crop)
        connection.verify(arguments.get('window_identity'))
    image=window_png(*pixels,crop['width'],crop['height'])
    return {'mime_type':'image/png','image_base64':base64.b64encode(image).decode(),'geometry':crop,'target_geometry':target}


def scoped_window_input(arguments):
    require(isinstance(arguments.get('window_identity'),dict) and set(arguments['window_identity'])=={'window_id','pid','start_ticks','boot_id','wm_class','instance_nonce'},'window_identity_required')
    action=arguments['action'];require(action in ['click','move','scroll'],'window_keyboard_input_not_isolated',501)
    with WindowConnection(arguments.get('window_id')) as connection:
        connection.verify(arguments.get('window_identity'));area=crop_box(arguments.get('crop'),**connection.bounds())
        x,y=arguments.get('x'),arguments.get('y');require(type(x) is int and type(y) is int and 0<=x<area['width'] and 0<=y<area['height'],'input_outside_target')
        x+=area['x'];y+=area['y']
        if action=='move':connection.event(6,x,y)
        else:
            amount=arguments.get('amount',3) if action=='scroll' else 1;direction=arguments.get('direction','down')
            require(type(amount) is int and 1<=amount<=20 and direction in ['up','down'])
            button=1 if action=='click' else 4 if direction=='up' else 5
            for _ in range(amount):
                connection.verify(arguments.get('window_identity'));connection.event(4,x,y,button);connection.event(5,x,y,button,1<<(button+7))
        connection.verify(arguments.get('window_identity'))
    return {'success':True}


def screen():
    raw = command(['xdotool', 'getdisplaygeometry']).split(); return {'width': int(raw[0]), 'height': int(raw[1])}


def window_bounds(window_id):
    require(isinstance(window_id, str) and re.fullmatch(r'0x[0-9a-fA-F]{1,16}', window_id), 'invalid_window_id')
    require(any(int(w['id'], 16) == int(window_id, 16) for w in windows()), 'window_not_found')
    info = command(['xwininfo', '-id', window_id])
    values = {}
    for name in ['Width', 'Height']:
        match = re.search(r'^\s*' + name + r':\s*(\d+)', info, re.MULTILINE); require(match, 'window_geometry_unavailable')
        values[name.lower()] = int(match[1])
    return values


def geometry(arguments):
    desktop_env(); width, height = arguments.get('width'), arguments.get('height')
    if width is None and height is None:return screen()
    require(type(width) is int and type(height) is int and 640 <= width <= 3840 and 480 <= height <= 2160 and width * height <= 8294400)
    # Xvnc --fb changes the backing framebuffer, while the active RandR
    # output (and desktop clients) can retain the old dimensions. Bind the
    # output to a real mode before confirming the desktop geometry.
    query = command(['xrandr', '--query'])
    outputs = re.findall(r'^([^\s]+) connected(?:\s|$)', query, re.MULTILINE)
    require(len(outputs) == 1, 'multi_output_geometry_unsupported', 501)
    output = outputs[0]; mode = 'MOLA-' + str(width) + 'x' + str(height)
    if not re.search(r'^\s+' + re.escape(mode) + r'\s', query, re.MULTILINE):
        hs, he, ht = width + 64, width + 128, width + 256
        vs, ve, vt = height + 3, height + 9, height + 35
        clock = format(ht * vt * 60 / 1000000, '.2f')
        command(['xrandr', '--newmode', mode, clock, str(width), str(hs), str(he), str(ht), str(height), str(vs), str(ve), str(vt), '-hsync', '+vsync'])
        command(['xrandr', '--addmode', output, mode])
    command(['xrandr', '--output', output, '--mode', mode])
    actual = screen(); require(actual == {'width': width, 'height': height}, 'geometry_change_not_applied', 422)
    save(state_dir() / 'geometry.json', actual)
    return actual


def capture(arguments):
    desktop_env(); mode = arguments.get('mode', 'desktop'); require(mode in ['desktop', 'window', 'browser'])
    if mode == 'browser':
        require(isinstance(arguments.get('browser_tab'), str) and arguments['browser_tab'], 'browser_tab_required')
        _, target, cdp = page({'tab_id': arguments.get('browser_tab')})
        try:
            try: return browser_capture(cdp, {**arguments, '_tab_id': target['id']})
            except ToolError as error:
                if error.code in ['browser_view_selector_unavailable','browser_view_geometry_changed']:return {'available':False,'reason':'target_moved' if error.code=='browser_view_geometry_changed' else 'target_not_visible','tab_id':target['id']}
                raise
        finally: cdp.close()
    if mode == 'window':
        return scoped_window_capture(arguments)
    else: target = screen(); width, height = target['width'], target['height']; window = 'root'
    crop = crop_box(arguments.get('crop'), width, height)
    image = command(['import', '-window', window, '-crop', '{width}x{height}+{x}+{y}'.format(**crop), '+repage', 'png:-'], binary=True)
    return {'mime_type': 'image/png', 'image_base64': base64.b64encode(image).decode(), 'geometry': crop,
            'target_geometry': {k: target[k] for k in ['width', 'height']}, 'window_id': window if mode == 'window' else None}


def vault(arguments):
    action = arguments.get('action'); require(action in ['browser_login', 'type_secret', 'captcha_inject'])
    if action == 'type_secret':
        desktop_env(); value = bounded_text(arguments.get('value'), 65536)
        expected = arguments.get('expected_app'); require(isinstance(expected, str) and expected, 'focused_app_identity_required')
        focused = command(['xdotool', 'getactivewindow']).strip()
        actual = command(['xprop', '-id', focused, 'WM_CLASS'])
        require(expected in re.findall(r'"([^"]+)"', actual), 'focused_app_mismatch')
        # Anonymous RAM only: an unlinked /tmp file can still leave password
        # bytes in free ext4 blocks copied by a filesystem checkpoint.
        require(hasattr(os,'memfd_create'),'private_memory_input_unsupported',501)
        descriptor=os.memfd_create('mola-private-input',getattr(os,'MFD_CLOEXEC',1))
        try:
            with os.fdopen(descriptor,'w+b',closefd=False) as stream:stream.write(value.encode());stream.flush();stream.seek(0)
            result = subprocess.run(['xdotool', 'type', '--window', focused, '--clearmodifiers', '--file', '/proc/self/fd/' + str(descriptor)], pass_fds=(descriptor,), capture_output=True, timeout=10)
            require(result.returncode == 0, 'secret_injection_failed', 422)
        finally:os.close(descriptor)
        return {'success': True}
    expected = web_url(arguments.get('url'), https=True)
    if action == 'captcha_inject': require(isinstance(arguments.get('tab_id'), str) and arguments['tab_id'], 'browser_tab_required')
    _, _, cdp = page(arguments)
    try:
        require(web_url(cdp.evaluate('location.href'), https=True) == expected, 'vault_browser_url_mismatch')
        if action == 'captcha_inject':
            field = arguments.get('field'); require(field in ['g-recaptcha-response', 'cf-turnstile-response'], 'unsupported_captcha_field')
            value = bounded_text(arguments.get('value'), 65536)
            cdp.evaluate('(()=>{if(location.href!==' + json.dumps(expected) + ')throw Error("url");const nodes=document.getElementsByName(' + json.dumps(field) + ');if(nodes.length!==1||!["INPUT","TEXTAREA"].includes(nodes[0].tagName))throw Error("field");const e=nodes[0];Object.getOwnPropertyDescriptor(e.tagName==="INPUT"?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,"value").set.call(e,' + json.dumps(value) + ');e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new Event("change",{bubbles:true}));return true;})()')
            return {'success': True}
        entries = []
        if arguments.get('username') is not None: entries.append((arguments.get('username_selector', 'input[autocomplete="username"],input[type="email"]'), bounded_text(arguments['username'], 8192)))
        entries.append((arguments.get('password_selector', 'input[type="password"]'), bounded_text(arguments.get('password'), 65536)))
        if arguments.get('totp') is not None:
            require(arguments.get('totp_selector'), 'totp_selector_required'); entries.append((arguments['totp_selector'], bounded_text(arguments['totp'], 32)))
        # Validate every selector first; then recheck exact URL and fill all
        # values in one synchronous browser turn. Return no DOM/value data.
        fields = [{'selector': bounded_text(selector, 1024), 'value': value} for selector, value in entries]
        submit = arguments.get('submit_selector')
        if submit is not None: bounded_text(submit, 1024)
        expression = "(() => {if(location.href!==" + json.dumps(expected) + ")throw Error('url');const fields=" + json.dumps(fields) + ";const nodes=fields.map(f=>{const a=[...document.querySelectorAll(f.selector)].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&!e.disabled});if(a.length!==1||![\"INPUT\",\"TEXTAREA\"].includes(a[0].tagName))throw Error('field');return a[0]});nodes.forEach((e,i)=>{Object.getOwnPropertyDescriptor(e.tagName===\"INPUT\"?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,\"value\").set.call(e,fields[i].value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))});"
        if submit:
            expression += "const buttons=document.querySelectorAll(" + json.dumps(submit) + ");if(buttons.length!==1)throw Error('submit');buttons[0].click();"
        cdp.evaluate(expression + 'return true;})()'); return {'success': True}
    finally: cdp.close()


def loopback_http(arguments):
    port, path = arguments.get('port'), arguments.get('path', '/')
    require(type(port) is int and 1024 <= port <= 65535 and port not in [4141, 4143, 5900, 6080, 9222, 18888], 'invalid_loopback_port')
    require(isinstance(path, str) and len(path) <= 4096 and path.startswith('/') and not path.startswith('//') and '\r' not in path and '\n' not in path, 'invalid_loopback_path')
    # The managed CDP listener is private to the browser tools, never an HTTP
    # escape hatch for a job to extract profile cookies or developer targets.
    try: _, cdp_port = browser_state()
    except ToolError: cdp_port = None
    require(port != cdp_port, 'managed_guest_port_forbidden')
    method = arguments.get('method', 'GET'); require(method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    body = arguments.get('body', '')
    if 'body' not in arguments and 'payload' in arguments: body = json.dumps(arguments['payload'])
    bounded_text(body, 1024 * 1024)
    headers = arguments.get('headers', {}); require(isinstance(headers, dict) and len(headers) <= 20)
    require(all(re.fullmatch(r'[A-Za-z0-9-]{1,64}', k) and k.lower() not in ['host', 'connection', 'proxy-authorization', 'transfer-encoding', 'content-length'] and isinstance(v, str) and '\n' not in v and '\r' not in v and len(v) <= 8192 for k,v in headers.items()))
    timeout = arguments.get('timeout_seconds', 15); require(type(timeout) is int and 1 <= timeout <= 30)
    if 'body' not in arguments and 'payload' in arguments and not any(k.lower()=='content-type' for k in headers): headers = {**headers, 'Content-Type': 'application/json'}
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method, path, body=body.encode() if body else None, headers=headers)
        response = conn.getresponse(); raw = response.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, 'http_response_too_large', 422)
        # Deliberately do not follow Location, or relay Set-Cookie/auth headers.
        return {'status_code': response.status, 'status': response.status, 'body': raw.decode('utf8', 'replace'), 'content_type': response.getheader('Content-Type')}
    finally: conn.close()


def package_name(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9+_.:-]{0,127}(?:=[a-zA-Z0-9+_.:~%-]{1,128})?', value), 'invalid_package_name')
    return value


def installer_spec(arguments):
    kind = arguments.get('kind'); require(kind in ['apt', 'pacman', 'deb', 'portable', 'flatpak', 'script'], 'unsupported_installer', 501)
    name = arguments.get('name'); require(isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', name), 'invalid_app_name')
    spec = {'kind': kind, 'name': name}
    deadline = arguments.get('timeout_seconds', 600); require(type(deadline) is int and 10 <= deadline <= 900)
    spec['timeout_seconds'] = deadline
    if kind in ['apt', 'pacman']:
        packages = arguments.get('packages'); require(isinstance(packages, list) and 1 <= len(packages) <= 20)
        spec['packages'] = [package_name(v) for v in packages]
    elif kind == 'flatpak':
        package = arguments.get('package'); require(isinstance(package, str) and re.fullmatch(r'[A-Za-z0-9_.-]{3,150}', package))
        spec['package'] = package
        require(arguments.get('remote', 'flathub') == 'flathub', 'unsupported_flatpak_remote', 501)
    elif kind in ['deb', 'portable']:
        spec['url'] = web_url(arguments.get('url'), https=True)
        checksum = arguments.get('sha256'); require(isinstance(checksum, str) and re.fullmatch(r'[a-f0-9]{64}', checksum), 'artifact_sha256_required')
        spec['sha256'] = checksum
        if kind == 'portable': spec['version'] = bounded_text(arguments.get('version'), 64)
    elif kind == 'script': spec['script'] = bounded_text(arguments.get('script'), 65536)
    if arguments.get('executable') is not None:
        executable = arguments['executable']; require(isinstance(executable, str) and re.fullmatch(r'[A-Za-z0-9_.+-]{1,128}', executable), 'invalid_app_executable')
        spec['executable'] = executable
    return spec


def install_worker(job_path):
    job_path = Path(job_path); job = read(job_path); spec = job['spec']
    def progress(status, **fields):
        job.update(status=status, updated_at=time.time(), **fields); save(job_path, job)
    progress('waiting', pid=os.getpid())
    try:
        with (state_dir() / 'install.lock').open('a+b') as lease:
            deadline = time.monotonic() + spec['timeout_seconds']
            while True:
                try: fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    require(time.monotonic() < deadline, 'installer_queue_timeout', 422); time.sleep(0.2)
            with tempfile.TemporaryDirectory(prefix='mola-install-') as scratch:
                artifact = Path(scratch) / 'artifact'
                kind = spec['kind']
                if kind in ['deb', 'portable']:
                    progress('downloading')
                    class NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *args, **kwargs): return None
                    with urllib.request.build_opener(NoRedirect).open(spec['url'], timeout=20) as response, artifact.open('wb') as target:
                        size = 0; digest = hashlib.sha256()
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk: break
                            size += len(chunk); require(size <= 512 * 1024 * 1024 and time.monotonic() < deadline, 'artifact_limit_exceeded', 422)
                            target.write(chunk); digest.update(chunk)
                    progress('validating'); require(digest.hexdigest() == spec['sha256'], 'artifact_checksum_mismatch', 422)
                if kind == 'portable':
                    releases = Path.home() / '.local/share/mola/apps' / spec['name'] / 'releases'; releases.mkdir(parents=True, exist_ok=True, mode=0o700)
                    require(re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', spec['version']), 'invalid_software_version')
                    destination = releases / spec['version']; require(not destination.exists(), 'software_version_already_staged')
                    destination.mkdir(mode=0o700); shutil.copyfile(artifact, destination / 'app'); (destination / 'app').chmod(0o700)
                    save(destination / 'release.json', {'version': spec['version'], 'sha256': spec['sha256']})
                    progress('completed', staged_version=spec['version'], executable_ready=True)
                    return
                env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
                if kind == 'apt': argv = ['sudo', '-n', 'apt-get', 'install', '-y', '--no-install-recommends', '--', *spec['packages']]
                elif kind == 'pacman': argv = ['sudo', '-n', 'pacman', '-S', '--noconfirm', '--needed', '--', *spec['packages']]
                elif kind == 'deb': argv = ['sudo', '-n', 'apt-get', 'install', '-y', '--', str(artifact)]
                elif kind == 'flatpak': argv = ['flatpak', 'install', '--user', '--noninteractive', '--', 'flathub', spec['package']]
                else:
                    script = Path(scratch) / 'setup.sh'; script.write_text(spec['script']); script.chmod(0o600); argv = ['/bin/bash', str(script)]
                def install_command(argv):
                    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
                    try: child.wait(timeout=max(1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL); child.wait(); raise ToolError('installer_timed_out', 422)
                    require(child.returncode == 0, 'installer_failed', 422)
                if kind == 'apt':
                    progress('refreshing_catalog'); install_command(['sudo','-n','apt-get','update','-q'])
                progress('installing'); install_command(argv)
                progress('launch_check')
                executable = spec.get('executable'); require(not executable or shutil.which(executable), 'app_not_launch_ready', 422)
                progress('completed', executable_ready=bool(executable))
                apps = read(state_dir() / 'apps.json', {}); apps[spec['name']] = {'kind': kind, 'executable': executable, 'installed_at': time.time()}; save(state_dir() / 'apps.json', apps)
    except ToolError as error: progress('failed', error_code=error.code)
    except Exception: progress('failed', error_code='installer_failed')


def app_status(operation_id):
    require(isinstance(operation_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id))
    path = state_dir() / 'installs' / (operation_id + '.json'); job = read(path); require(job is not None, 'install_operation_not_found')
    status = job['status']
    if status not in ['completed', 'failed']:
        process = Path('/proc') / str(job.get('pid', 0)) / 'cmdline'
        if status == 'queued':
            if time.time() - job['created_at'] > 10: status = 'outcome_unknown'
        elif not process.exists() or str(path).encode() not in process.read_bytes().split(b'\0'): status = 'outcome_unknown'
    return {'operation_id': operation_id, 'name': job['spec']['name'], 'status': status,
            **{k:job[k] for k in ['error_code', 'executable_ready', 'staged_version'] if k in job}}


def apps(tool, arguments):
    if tool == 'app_list': return {'apps': [{'name': k, **v} for k,v in read(state_dir() / 'apps.json', {}).items()]}
    if tool == 'app_status': return app_status(arguments.get('operation_id'))
    if tool == 'app_launch':
        registry = read(state_dir() / 'apps.json', {}); app = registry.get(arguments.get('name')); require(app and app.get('executable'), 'app_not_registered_or_ready')
        desktop_env(); args = arguments.get('args', []); require(isinstance(args, list) and len(args) <= 20); [bounded_text(v, 8192) for v in args]
        child = subprocess.Popen([app['executable'], *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(0.2); require(child.poll() is None or child.returncode == 0, 'app_launch_failed', 422)
        return {'launched': True, 'name': arguments['name']}
    require(tool == 'app_install', 'unsupported_computer_tool')
    spec = installer_spec(arguments); identity = arguments.get('operation_id'); require(isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity), 'installer_operation_id_required')
    folder = state_dir() / 'installs'; folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (identity + '.json'); fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    if path.exists():
        require(read(path)['fingerprint'] == fingerprint, 'installer_operation_payload_mismatch'); return app_status(identity)
    active = [p for p in folder.glob('*.json') if read(p)['status'] not in ['completed', 'failed']]
    require(len(active) < 4, 'installer_queue_full', 422)
    save(path, {'fingerprint': fingerprint, 'spec': spec, 'status': 'queued', 'created_at': time.time()})
    source = globals().get('__source__'); require(source, 'installer_source_unavailable', 503)
    script = folder / ('worker-' + hashlib.sha256(source.encode()).hexdigest() + '.py')
    if not script.exists():
        script.write_text(source + '\nif __name__ == "__main__":\n import sys\n install_worker(sys.argv[1])\n'); script.chmod(0o600)
    child = subprocess.Popen(['python3', str(script), str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    # The accepted identity is durable before process launch. Retrying never
    # repeats a package/script side effect whose prior outcome is unknown.
    return {'operation_id': identity, 'name': spec['name'], 'status': 'queued'}


def software(tool, arguments):
    name = arguments.get('name'); require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name), 'invalid_app_name')
    root = Path.home() / '.local/share/mola/apps' / name
    policy_path = state_dir() / 'software.json'; policies = read(policy_path, {}); policy = policies.get(name, {'pin': None})
    pending = policy.get('pending_activation')
    if pending:
        current = root / 'current'
        if current.is_symlink() and current.resolve() == (root / 'releases' / pending['version']).resolve():
            policy.update(current=pending['version'], previous=pending.get('previous')); del policy['pending_activation']
            policies[name] = policy; save(policy_path, policies)
    if tool == 'software_pin':
        require(name not in ['chrome','cua-driver'],'native_software_update_unsupported',501)
        pin = arguments.get('version'); require(pin is None or isinstance(pin, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', pin))
        policy['pin'] = pin
    elif tool == 'software_stage': return apps('app_install', {**arguments, 'kind': 'portable'})
    elif tool in ['software_activate', 'software_rollback']:
        version = arguments.get('version') if tool == 'software_activate' else policy.get('previous')
        require(isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', version), 'rollback_or_version_unavailable')
        require(policy.get('pin') is None or policy['pin'] == version, 'software_version_pinned')
        release = root / 'releases' / version; metadata = read(release / 'release.json')
        require(metadata and (release / 'app').is_file() and hashlib.sha256((release / 'app').read_bytes()).hexdigest() == metadata['sha256'], 'staged_software_validation_failed')
        # Probe before publishing. A failed replacement leaves current and all
        # customer files/profile untouched. Portable apps must support --version.
        command([str(release / 'app'), '--version'], timeout=10)
        current = root / 'current'; temporary = root / ('.current-' + uuid.uuid4().hex); temporary.symlink_to('releases/' + version)
        require(not current.exists() or current.is_symlink(), 'unmanaged_software_current_path')
        policy['pending_activation'] = {'version': version, 'previous': policy.get('current')}; policies[name] = policy; save(policy_path, policies)
        policy['previous'] = policy.get('current'); os.replace(temporary, current); policy['current'] = version
        descriptor = os.open(root, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        del policy['pending_activation']
        registered = read(state_dir() / 'apps.json', {}); registered[name] = {'kind': 'portable', 'executable': str(current / 'app')}; save(state_dir() / 'apps.json', registered)
    else: require(tool == 'software_state', 'unsupported_computer_tool')
    policies[name] = policy; save(policy_path, policies)
    native_paths = {'chrome': '/opt/google/chrome/google-chrome', 'cua-driver': '/usr/local/bin/cua-driver'}
    native = native_paths.get(name); version = policy.get('current')
    if native and Path(native).is_file():
        version = command([native, '--version']).strip()[:256]
    return {'name': name, **policy, 'installed_version': version, 'update_supported': native is None,
            'staged_versions': sorted(p.name for p in (root / 'releases').glob('*') if p.is_dir())}


SESSION_INSTALL=r'''
import os,sys,json,pathlib,tempfile
data=json.load(sys.stdin);base=pathlib.Path('/usr/local/lib/mola')
for p in [base.parent.parent,base.parent,base]:
 if p.is_symlink():sys.exit(2)
 if not p.exists():p.mkdir(mode=0o755)
 if not p.is_dir() or p.stat().st_uid!=0 or p.stat().st_mode&0o022:sys.exit(2)
for name,key in [('guest_tools.py','tools'),('session_agent.py','agent')]:
 path=base/name
 if path.is_symlink():sys.exit(2)
 source=data[key].encode();assert len(source)<=262144
 fd,tmp=tempfile.mkstemp(prefix='.'+name,dir=base)
 with os.fdopen(fd,'wb') as f:f.write(source);f.flush();os.fsync(f.fileno())
 os.chmod(tmp,0o644);os.replace(tmp,path)
fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
'''


def session_autosave(arguments):
    desktop_env();enabled=arguments.get('enabled');require(type(enabled) is bool)
    interval=arguments.get('interval_seconds',60);require(type(interval) is int and 30<=interval<=300)
    unit_dir=Path.home()/'.config/systemd/user';unit=unit_dir/'mola-session-autosave.service'
    require(not unit_dir.is_symlink() and not unit.is_symlink(),'unsafe_session_service')
    if enabled:
        source,agent=globals().get('__source__'),globals().get('__session_source__');require(source and agent,'session_agent_source_unavailable',503)
        result=subprocess.run(['sudo','-n','python3','-c',SESSION_INSTALL],input=json.dumps({'tools':source,'agent':agent}),capture_output=True,text=True,timeout=10)
        require(result.returncode==0,'session_agent_install_failed',503)
        names=arguments.get('apps',read(state_dir()/'session.json',{}).get('apps',[]));registry=read(state_dir()/'apps.json',{})
        require(isinstance(names,list) and len(names)<=20 and all(n in registry for n in names),'unregistered_session_app')
        config=state_dir()/'session-agent-config.json';save(config,{'apps':names})
        unit_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
        content='[Unit]\nDescription=Mola managed session autosave\nAfter=graphical-session.target\n[Service]\nType=simple\nEnvironment=DISPLAY=:1\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nExecStart=/usr/bin/python3 /usr/local/lib/mola/session_agent.py --interval '+str(interval)+' --apps-json '+str(config)+' --restore-first\nRestart=on-failure\nRestartSec=10\nStandardOutput=null\nStandardError=null\n[Install]\nWantedBy=default.target\n'
        unit.write_text(content);unit.chmod(0o600)
        command(['systemctl','--user','daemon-reload']);command(['systemctl','--user','enable',unit.name]);command(['systemctl','--user','restart',unit.name])
        require(command(['systemctl','--user','is-active',unit.name]).strip()=='active','session_autosave_not_active',503)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status=read(state_dir()/'session-agent-status.json',{})
            if status.get('restore_state')=='restored':break
            time.sleep(.1)
        require(status.get('restore_state')=='restored','session_autosave_restore_unconfirmed',503)
    elif unit.exists():command(['systemctl','--user','disable','--now',unit.name])
    return {'enabled':enabled,'interval_seconds':interval if enabled else None}


def sessions(tool, arguments):
    path = state_dir() / 'session.json'
    if tool == 'session_autosave':return session_autosave(arguments)
    if tool == 'session_state': return {'manifest': read(path),'autosave':read(state_dir()/'session-agent-status.json')}
    if tool == 'session_save':
        browser_tabs = []; browser_mode = None
        try:
            state, pages = targets(); browser_mode = state['mode']; browser_tabs = [{'url': p['url'], 'active': p['id'] == state.get('current_tab')} for p in pages if p['url'].startswith(('http://', 'https://'))]
        except ToolError: pass
        try: app_windows = session_windows()
        except ToolError: app_windows = []
        names = arguments.get('apps', []); require(isinstance(names, list) and len(names) <= 20)
        registry = read(state_dir() / 'apps.json', {}); require(all(n in registry for n in names), 'unregistered_session_app')
        manifest = {'schema': 1, 'saved_at': time.time(), 'tabs': browser_tabs, 'browser_mode': browser_mode, 'apps': names,
                    'windows': [{k:w[k] for k in ['id', 'x', 'y', 'width', 'height', 'wm_class'] if k in w} for w in app_windows]}
        save(path, manifest); return {'manifest': manifest}
    require(tool == 'session_restore', 'unsupported_computer_tool'); manifest = read(path); require(manifest is not None, 'session_manifest_not_found')
    errors = []; reopened = 0
    if manifest.get('browser_mode'):
        try:
            prepare({'mode': manifest['browser_mode']}); existing = browser('browser_tabs', {})['tabs']; present = [p['url'] for p in existing]
            for tab in manifest['tabs']:
                if tab['url'] not in present:
                    opened = browser('browser_tabs', {'action': 'new', 'url': tab['url']}); reopened += 1
                    if tab.get('active'): browser('browser_tabs', {'action': 'select', 'tab_id': opened['current_tab']})
        except ToolError: errors.append('browser_restore_unavailable')
    for name in manifest['apps']:
        try: apps('app_launch', {'name': name})
        except ToolError: errors.append('app_restore_unavailable')
    # XIDs change across boots. Bind layout restoration to a unique app class;
    # ambiguous/missing windows are reported rather than moving another app.
    frames=[w for w in manifest.get('windows',[]) if isinstance(w.get('wm_class'),list) and len(w['wm_class'])==2]
    restored_windows=0
    if frames:
        deadline=time.monotonic()+5;current=[]
        while time.monotonic()<deadline:
            try:current=session_windows()
            except ToolError:break
            if all(any(w.get('wm_class')==f['wm_class'] for w in current) for f in frames):break
            time.sleep(.1)
        desktop=screen()
        for frame in frames:
            candidates=[w for w in current if w.get('wm_class')==frame['wm_class']]
            if len(candidates)!=1 or sum(f['wm_class']==frame['wm_class'] for f in frames)!=1:
                errors.append('window_restore_unavailable');continue
            width=max(100,min(desktop['width'],frame['width']));height=max(80,min(desktop['height'],frame['height']))
            x=max(0,min(desktop['width']-width,frame['x']));y=max(0,min(desktop['height']-height,frame['y']))
            try:
                command(['xdotool','windowsize',candidates[0]['id'],str(width),str(height)])
                command(['xdotool','windowmove',candidates[0]['id'],str(x),str(y)]);restored_windows+=1
            except ToolError:errors.append('window_restore_unavailable')
    if not errors and Path('/proc/sys/kernel/random/boot_id').exists():
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        save(state_dir()/'session-agent-status.json',{'schema':1,'checked_at':time.time(),'restore_state':'restored','restore_boot_id':boot,'restore_submitted_boot_id':boot})
    return {'restored': not errors, 'reopened_tabs': reopened, 'restored_windows':restored_windows,'errors': errors}


SCOPED_DOM_INPUT=r'''(url,selector,area,input)=>{
 if(location.href!==url)return false;
 const es=document.querySelectorAll(selector);if(es.length!==1)return false;
 const root=es[0],visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return s.visibility==='visible'&&s.display!=='none'&&r.width>0&&r.height>0&&r.left>=area.x&&r.top>=area.y&&r.right<=area.x+area.width&&r.bottom<=area.y+area.height};
 if(input.action==='click'||input.action==='move'||input.action==='scroll'){
  const e=document.elementFromPoint(area.x+input.x,area.y+input.y);if(!e||!root.contains(e))return false;
  if(input.action==='click'){e.focus();e.click();}
  else if(input.action==='scroll')root.scrollBy({top:input.delta_y});
  else e.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:area.x+input.x,clientY:area.y+input.y}));
  return true;
 }
 let e=document.activeElement;if(!e||!root.contains(e)||!visible(e))return false;
 if(input.action==='key'&&input.key==='Tab'){
  const list=[root,...root.querySelectorAll('input,textarea,button,select,a[href],[tabindex]')].filter(e=>!e.disabled&&e.tabIndex>=0&&visible(e));
  if(!list.length)return false;list[(list.indexOf(e)+1)%list.length].focus();return true;
 }
 if(input.action==='key'&&input.key==='Enter'){
  if(e.tagName==='BUTTON'){e.click();return true;}
  if(e.tagName==='INPUT'&&e.form&&root.contains(e.form)){e.form.requestSubmit();return true;}
 }
 if(!['INPUT','TEXTAREA'].includes(e.tagName)||e.disabled||e.readOnly||e.selectionStart===null)return false;
 let start=e.selectionStart,end=e.selectionEnd,value=e.value,insert=input.action==='type'?input.text:'';
 if(input.action==='key'){
  if(input.key==='Backspace'&&start===end)start=Math.max(0,start-1);
  else if(input.key==='Delete'&&start===end)end=Math.min(value.length,end+1);
  else if(['ArrowLeft','ArrowRight','Home','End'].includes(input.key)){
   const pos=input.key==='Home'?0:input.key==='End'?value.length:input.key==='ArrowLeft'?Math.max(0,start-1):Math.min(value.length,end+1);e.setSelectionRange(pos,pos);return true;
  }else if(input.key==='Enter'&&e.tagName==='TEXTAREA')insert='\n';
  else if(!['Backspace','Delete'].includes(input.key))return false;
 }
 const setter=Object.getOwnPropertyDescriptor(e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype,'value').set;
 setter.call(e,value.slice(0,start)+insert+value.slice(end));e.setSelectionRange(start+insert.length,start+insert.length);
 e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true;
}'''


def view_input(arguments):
    # A server must bind these constraints to its viewer grant. This endpoint
    # additionally directs input only to the target and excludes global keys.
    desktop_env(); require(arguments.get('watch_only', False) is False, 'viewer_watch_only', 409)
    if 'scope' in arguments:
        require(isinstance(arguments['scope'], dict) and isinstance(arguments.get('input'), dict))
        require(not (set(arguments['scope']) & set(arguments['input'])), 'viewer_input_scope_override')
        arguments = {**arguments['scope'], **arguments['input']}
    mode, action = arguments.get('mode'), arguments.get('action'); require(action in ['click', 'move', 'scroll', 'type', 'key'], 'restricted_view_input_unsupported', 501)
    if mode == 'browser':
        require(isinstance(arguments.get('browser_tab'), str) and arguments['browser_tab'], 'browser_tab_required')
        _, _, cdp = page({'tab_id': arguments.get('browser_tab')})
        try:
            metadata,bounds,area = browser_area(cdp,arguments)
            if arguments.get('selector') is not None:
                # DOM execution is tied to this document and rechecks URL,
                # selector and focused descendant atomically. OS keyboard/CDP
                # global input cannot escape the grant's selector boundary.
                input_data={k:arguments.get(k) for k in ['action','text','key','x','y','delta_y']}
                if action=='type':bounded_text(input_data['text'],8192)
                if action=='key':require(input_data['key'] in ['Enter','Tab','Backspace','Delete','ArrowLeft','ArrowRight','Home','End'],'restricted_view_input_unsupported',501)
                if action not in ['type','key']:
                    require(type(input_data['x']) is int and type(input_data['y']) is int and 0<=input_data['x']<area['width'] and 0<=input_data['y']<area['height'],'input_outside_target')
                if action=='scroll':require(type(input_data['delta_y']) is int and -2000<=input_data['delta_y']<=2000)
                expression='('+SCOPED_DOM_INPUT+')('+','.join(json.dumps(v) for v in [metadata['url'],arguments['selector'],area,input_data])+')'
                require(cdp.evaluate(expression) is True,'browser_view_focus_outside_selector',409)
                return {'success':True}
            if action in ['type', 'key']:
                require(arguments.get('crop') is None, 'cropped_keyboard_input_not_isolated', 501)
                if action == 'type': cdp.call('Input.insertText', {'text': bounded_text(arguments.get('text'), 8192)})
                else:
                    key = bounded_text(arguments.get('key'), 32); require(key in ['Enter', 'Tab', 'Backspace', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'])
                    for event in ['keyDown', 'keyUp']: cdp.call('Input.dispatchKeyEvent', {'type': event, 'key': key})
            else:
                x, y = arguments.get('x'), arguments.get('y'); require(type(x) is int and type(y) is int and 0 <= x < area['width'] and 0 <= y < area['height'], 'input_outside_target')
                if action == 'scroll':
                    delta = arguments.get('delta_y'); require(type(delta) is int and -2000 <= delta <= 2000)
                    cdp.call('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': area['x']+x, 'y': area['y']+y, 'deltaX': 0, 'deltaY': delta})
                else:
                    for event in (['mouseMoved'] if action == 'move' else ['mousePressed', 'mouseReleased']): cdp.call('Input.dispatchMouseEvent', {'type': event, 'x': area['x'] + x, 'y': area['y'] + y, 'button': 'left', 'clickCount': 1})
            return {'success': True}
        finally: cdp.close()
    require(mode in ['window', 'desktop'], 'restricted_view_input_unsupported', 501)
    if mode=='window':return scoped_window_input(arguments)
    window = arguments.get('window_id') if mode == 'window' else 'root'
    bounds = window_bounds(window) if mode == 'window' else screen()
    area = crop_box(arguments.get('crop'), **bounds)
    if action in ['type', 'key']:
        require(mode == 'desktop' and arguments.get('crop') is None, 'window_keyboard_input_not_isolated', 501)
        if action == 'key':
            key = bounded_text(arguments.get('key'), 64); require(re.fullmatch(r'[A-Za-z0-9_+.-]{1,64}', key)); command(['xdotool', 'key', '--clearmodifiers', key])
        else:
            text = bounded_text(arguments.get('text'), 8192)
            command(['xdotool', 'type', '--clearmodifiers', '--', text])
    else:
        x,y = arguments.get('x'), arguments.get('y'); require(type(x) is int and type(y) is int and 0 <= x < area['width'] and 0 <= y < area['height'], 'input_outside_target')
        command(['xdotool', 'mousemove', '--window', window, str(area['x']+x), str(area['y']+y)])
        if action == 'click': command(['xdotool', 'click', '--window', window, '1'])
        if action == 'scroll':
            amount = arguments.get('amount', 3); direction = arguments.get('direction', 'down'); require(type(amount) is int and 1 <= amount <= 20 and direction in ['up','down'])
            command(['xdotool', 'click', '--window', window, '--repeat', str(amount), '4' if direction == 'up' else '5'])
    return {'success': True}


NETWORK_ROOT = r'''
import hashlib,json,os,pathlib,subprocess,sys
p=json.load(sys.stdin)
config=pathlib.Path('/etc/mola/browser-proxy.json')
policy=pathlib.Path('/etc/opt/chrome/policies/managed/mola-browser-proxy.json')
helper=pathlib.Path('/usr/local/bin/mola-browser-proxy')
service=pathlib.Path('/etc/systemd/system/mola-browser-proxy.service')
meter=pathlib.Path('/var/lib/mola/browser-proxy/counters.json')
binding_path=pathlib.Path('/etc/mola/browser-proxy-binding.json')
binding=json.loads(binding_path.read_text()) if binding_path.exists() and not binding_path.is_symlink() else {}
if p['tool']=='network_status':
 counters=json.loads(meter.read_text()) if meter.exists() and not meter.is_symlink() else None
 active=subprocess.run(['systemctl','is-active','--quiet','mola-browser-proxy.service'],capture_output=True).returncode==0
 state=binding.get('status','configured' if config.exists() else 'disabled')
 print(json.dumps({'status':state,'configured':config.exists() and state=='configured','configuration_id':binding.get('configuration_id'),'country':binding.get('country'),'operation_id':binding.get('operation_id'),'service_active':active,'provider':'decodo' if config.exists() else None,'scope':'browser_and_proxy_aware_apps','raw_socket_bypass':True,'tls_unlock':False,'metering_supported':counters is not None,'metrics':counters}))
else:
 for path in [config,policy,helper,service,meter,binding_path]:
  if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):raise ValueError('unsafe managed file')
 if helper.exists() and not helper.read_text().startswith('"""Loopback-only HTTP/CONNECT bridge'):raise ValueError('unmanaged proxy helper')
 if service.exists() and 'ExecStart=/usr/bin/python3 /usr/local/bin/mola-browser-proxy' not in service.read_text():raise ValueError('unmanaged proxy service')
 namespace={'__name__':'mola_proxy'};exec(p['proxy_source'],namespace)
 value=p.get('browser_proxy')
 if value is not None:namespace['validate'](value)
 if p['tool']=='network_teardown' and binding.get('configuration_id')!=p.get('configuration_id'):raise ValueError('configuration identity mismatch')
 fingerprint=hashlib.sha256(json.dumps({'config':value,'binding':p.get('binding')},sort_keys=True).encode()).hexdigest()
 if value is not None and binding.get('configuration_id')==p['binding']['configuration_id']:
  if binding.get('payload_fingerprint',fingerprint)!=fingerprint or any(binding.get(k)!=v for k,v in p['binding'].items()):raise ValueError('operation payload mismatch')
  if binding.get('status','configured')=='configured':
   if not config.exists() or json.loads(config.read_text())!=value:raise ValueError('operation payload mismatch')
   print(json.dumps({'status':'configured','configured':True,'configuration_id':binding['configuration_id'],'scope':'browser_and_proxy_aware_apps','raw_socket_bypass':True,'tls_unlock':False}));sys.exit(0)
 def write(path,text,mode):
  path.parent.mkdir(parents=True,exist_ok=True)
  temporary=path.with_name(path.name+'.new')
  if temporary.is_symlink():raise ValueError('unsafe stage')
  with temporary.open('w') as f:f.write(text);f.flush();os.fsync(f.fileno())
  temporary.chmod(mode);temporary.replace(path)
  descriptor=os.open(path.parent,os.O_RDONLY)
  try:os.fsync(descriptor)
  finally:os.close(descriptor)
 if value is None:
  write(binding_path,json.dumps({**binding,'status':'teardown_pending'}),0o600)
  subprocess.run(['systemctl','stop','mola-browser-proxy.service'],capture_output=True,check=True)
  config.unlink(missing_ok=True);policy.unlink(missing_ok=True)
  for parent in [config.parent,policy.parent]:
   descriptor=os.open(parent,os.O_RDONLY)
   try:os.fsync(descriptor)
   finally:os.close(descriptor)
  write(binding_path,json.dumps({**binding,'status':'disabled'}),0o600)
 else:
  write(binding_path,json.dumps({**p['binding'],'status':'pending','payload_fingerprint':fingerprint}),0o600)
  write(helper,p['proxy_source'],0o644)
  write(config,json.dumps(value),0o600)
  write(policy,json.dumps({'ProxySettings':{'ProxyMode':'fixed_servers','ProxyServer':'http://127.0.0.1:18888','ProxyBypassList':'localhost;127.0.0.1;[::1]'},'WebRtcIPHandling':'disable_non_proxied_udp','QuicAllowed':False}),0o644)
  folder=pathlib.Path('/var/lib/mola/browser-proxy');folder.mkdir(parents=True,exist_ok=True)
  write(service,'[Unit]\nDescription=Mola browser proxy\nAfter=network-online.target\nWants=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/mola-browser-proxy\nRestart=always\nRestartSec=2\nNoNewPrivileges=true\nProtectSystem=strict\nProtectHome=true\nPrivateTmp=true\nReadWritePaths=/var/lib/mola/browser-proxy\n[Install]\nWantedBy=multi-user.target\n',0o644)
  subprocess.run(['systemctl','daemon-reload'],capture_output=True,check=True)
  subprocess.run(['systemctl','enable','--now','mola-browser-proxy.service'],capture_output=True,check=True)
  subprocess.run(['systemctl','restart','mola-browser-proxy.service'],capture_output=True,check=True)
  if subprocess.run(['systemctl','is-active','--quiet','mola-browser-proxy.service'],capture_output=True).returncode:raise ValueError('proxy inactive')
  write(binding_path,json.dumps({**p['binding'],'status':'configured','payload_fingerprint':fingerprint}),0o600)
 print(json.dumps({'status':'configured' if value is not None else 'disabled','configured':value is not None,'configuration_id':p['binding']['configuration_id'] if value is not None else p['configuration_id'],'scope':'browser_and_proxy_aware_apps','raw_socket_bypass':True,'tls_unlock':False}))
'''


def network(tool, arguments):
    require(tool in ['network_configure', 'network_status', 'network_teardown'], 'unsupported_computer_tool')
    require(not arguments.get('tls_unlock'), 'tls_unlock_not_qualified', 501)
    require(Path('/opt/google/chrome/google-chrome').is_file(), 'network_image_unsupported', 501)
    supplied = arguments.get('browser_proxy')
    identity = supplied.get('configuration_id') if isinstance(supplied, dict) else arguments.get('configuration_id')
    if tool != 'network_status':
        require(arguments.get('allow_restart') is True, 'network_restart_consent_required')
        require(isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity), 'network_configuration_identity_required')
        require(isinstance(arguments.get('operation_id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', arguments['operation_id']), 'network_operation_identity_required')
    if tool == 'network_configure':
        require(isinstance(supplied, dict) and set(supplied) == {'provider','host','port','username','password','country','configuration_id'}, 'invalid_proxy_configuration')
        require(isinstance(supplied['country'], str) and re.fullmatch(r'[A-Z]{2}', supplied['country']), 'invalid_proxy_country')
    stopped = False; old_mode = 'full'
    if tool != 'network_status':
        for pid, raw in list(chrome_processes()):
            state = read(state_dir() / 'browser.json', {})
            require(arguments.get('allow_restart') is True and pid == state.get('pid') and profile_process(pid, state['profile']), 'browser_must_close_before_network_change')
            old_mode = state['mode']; os.kill(pid, signal.SIGTERM)
            for _ in range(100):
                if not profile_process(pid, state['profile']): break
                time.sleep(0.05)
            require(not profile_process(pid, state['profile']), 'browser_shutdown_outcome_unknown', 503); stopped = True
    proxy = {k:supplied[k] for k in ['provider','host','port','username','password']} if tool == 'network_configure' else None
    metadata = {'configuration_id': identity, 'country': supplied['country'], 'operation_id': arguments['operation_id']} if tool == 'network_configure' else None
    data = {'tool': tool, 'browser_proxy': proxy, 'binding': metadata, 'configuration_id': identity, 'proxy_source': globals().get('__proxy_source__')}
    require(tool != 'network_configure' or isinstance(data['browser_proxy'], dict), 'browser_proxy_required')
    require(tool == 'network_status' or data['proxy_source'], 'managed_proxy_source_unavailable', 503)
    try:
        response = subprocess.run(['sudo','-n','python3','-c',NETWORK_ROOT], input=json.dumps(data), capture_output=True, text=True, timeout=20)
        require(response.returncode == 0, 'network_configuration_failed', 422)
        result = json.loads(response.stdout)
    except (OSError, subprocess.SubprocessError, ValueError): raise ToolError('network_configuration_outcome_unknown', 503) from None
    if stopped: prepare({'mode': old_mode})
    return result


def dispatch(request):
    try:
        require(isinstance(request, dict)); kind = request.get('kind')
        arguments = request.get('arguments', {}) if request.get('tool') else {k:v for k,v in request.items() if k not in ['kind', 'binding']}
        require(isinstance(arguments, dict))
        if kind == 'browser': result = browser(request['tool'], arguments)
        elif kind == 'geometry': result = geometry(arguments)
        elif kind == 'capture': result = capture(arguments)
        elif kind == 'vault-inject': result = vault(arguments)
        elif kind == 'apps': result = apps(request['tool'], arguments)
        elif kind == 'session-manifest': result = sessions(request['tool'], arguments)
        elif kind == 'view-input': result = view_input(arguments)
        elif kind == 'network-tools': result = network(request['tool'], arguments)
        elif kind == 'computer-tools': result = loopback_http(arguments) if request['tool'] == 'loopback_http' else view_input(arguments) if request['tool'] == 'viewer_input' else window_identity(arguments) if request['tool']=='window_identity' else window_prepare(arguments) if request['tool']=='window_prepare' else software(request['tool'], arguments)
        else: raise ToolError('unsupported_computer_tool', 501)
        return {'ok': True, 'result': result}
    except ToolError as error: return {'ok': False, 'status': error.status, 'code': error.code}
    except Exception: return {'ok': False, 'status': 503, 'code': 'guest_tool_failed'}
