"""Opt-in managed session autosave. No profile copying or implicit app launch.

Install root-owned beside guest_tools.py. The user service serializes its own
cycles with flock, preserves unavailable components, and publishes only bounded
complete JSON. Exceptions and captured URLs/titles never enter service logs.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

MAX_BYTES = 262144


def private_state(home):
    home = Path(home)
    for directory in [home, home/'.local', home/'.local/state', home/'.local/state/mola']:
        if directory.is_symlink(): raise ValueError('Unsafe session directory')
        directory.mkdir(mode=0o700, exist_ok=True)
        # Standard XDG ancestors may be group-writable on the single-user
        # guest image. The actual state leaf and all files remain private.
        if not directory.is_dir() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o002: raise ValueError('Unsafe session directory')
    if stat.S_IMODE((home/'.local/state/mola').stat().st_mode) != 0o700: raise ValueError('Session directory is not private')
    return home/'.local/state/mola'


def read_private(path, fallback=None):
    path = Path(path)
    if not path.exists():
        if path.is_symlink(): raise ValueError('Unsafe session file')
        return fallback
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_BYTES:
        raise ValueError('Unsafe session file')
    return json.loads(path.read_text())


def atomic(path, value):
    encoded = json.dumps(value, separators=(',', ':')).encode()
    if len(encoded) > MAX_BYTES: raise ValueError('Session manifest exceeds bound')
    if Path(path).is_symlink(): raise ValueError('Unsafe session publication')
    temporary = Path(str(path)+'.'+str(os.getpid())+'.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def valid_manifest(value):
    if value is None: return None
    if not isinstance(value, dict) or value.get('schema') != 1 or not all(isinstance(value.get(k), list) for k in ['tabs', 'windows', 'apps']):
        raise ValueError('Invalid prior session')
    if len(value['tabs']) > 100 or len(value['windows']) > 100 or len(value['apps']) > 20: raise ValueError('Invalid prior session')
    return value


def boot_identity():
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value): raise ValueError('Invalid boot identity')
    return value


def wait_desktop(tools, timeout=120, sleep=time.sleep, monotonic=time.monotonic):
    deadline = monotonic()+timeout
    while True:
        try:
            tools.desktop_env()
            size = tools.screen()
            if isinstance(size, dict) and type(size.get('width')) is int and type(size.get('height')) is int \
                    and 640 <= size['width'] <= 3840 and 480 <= size['height'] <= 2160:
                return
        except Exception: pass
        if monotonic() >= deadline: raise ValueError('Desktop readiness is not established')
        sleep(min(2, max(0, deadline-monotonic())))


def restore_first(tools, folder, boot=None):
    """Per-boot restoration is fenced BEFORE launch, never replayed if unknown."""
    boot = boot or boot_identity()
    path = folder/'session-agent-status.json'
    status = read_private(path, {})
    if not isinstance(status, dict): raise ValueError('Invalid session status')
    if status.get('restore_boot_id') == boot: return
    if status.get('restore_submitted_boot_id') == boot:
        raise ValueError('Session restoration outcome is unknown')
    manifest = valid_manifest(read_private(folder/'session.json'))
    # User default.target can precede the actual display service. An absent
    # display is positively unsubmitted work, not an unknown app launch.
    wait_desktop(tools)
    progress = {'schema': 1, 'checked_at': time.time(), 'restore_submitted_boot_id': boot, 'restore_state': 'submitted'}
    atomic(path, progress)
    if manifest is not None:
        # A failure can mean apps launched before an acknowledgement was lost.
        # Keep submitted identity and the manifest, rather than launching again.
        try:
            result = tools.sessions('session_restore', {})
            if not isinstance(result, dict) or result.get('restored') is not True: raise ValueError()
        except Exception:
            progress['restore_state'] = 'unknown'; atomic(path, progress)
            raise ValueError('Session restoration outcome is unknown') from None
    progress.update(restore_boot_id=boot, restore_state='restored')
    atomic(path, progress)


def capture(tools, folder, apps_path=None, clock=time.time):
    """One observational cycle. A missing probe never replaces good data."""
    path = folder/'session.json'
    previous = valid_manifest(read_private(path))
    manifest = dict(previous or {'schema': 1, 'tabs': [], 'windows': [], 'apps': [], 'browser_mode': None})
    probes = {'browser': False, 'windows': False, 'apps': apps_path is None or not apps_path.exists() and not apps_path.is_symlink()}
    try:
        browser, pages = tools.targets()
        if not isinstance(browser, dict) or browser.get('mode') not in ('full', 'lightweight') or not isinstance(pages, list) or len(pages) > 100:
            raise ValueError('Invalid browser session')
        tabs = []
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get('url'), str): raise ValueError('Invalid browser page')
            if not page['url'].startswith(('http://', 'https://')): continue
            try: url = tools.web_url(page['url'])
            except Exception: continue
            tabs.append({'url': url, 'active': page.get('id') == browser.get('current_tab')})
        manifest.update(tabs=tabs, browser_mode=browser['mode']); probes['browser'] = True
    except Exception: pass
    try:
        raw = tools.session_windows() if callable(getattr(tools,'session_windows',None)) else tools.windows()
        if not isinstance(raw, list) or len(raw) > 100: raise ValueError('Invalid window list')
        windows = []
        for window in raw:
            if not isinstance(window, dict) or not isinstance(window.get('id'), str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,16}', window['id']): raise ValueError('Invalid window identity')
            frame = {'id': window['id']}
            for key in ['x', 'y', 'width', 'height']:
                value = window.get(key)
                if type(value) is not int or not -65536 <= value <= 65536 or key in ('width', 'height') and value < 1:
                    raise ValueError('Invalid window bounds')
                frame[key] = value
            # Optional immutable app identity supplied by the managed tool's
            # restore integration. Never retain window titles or arbitrary data.
            for key in ['app', 'wm_class']:
                value = window.get(key)
                if isinstance(value, str) and 0 < len(value) <= 128: frame[key] = value
                elif isinstance(value, list) and len(value) <= 4 and all(isinstance(v, str) and 0 < len(v) <= 128 for v in value): frame[key] = value
            windows.append(frame)
        manifest['windows'] = windows; probes['windows'] = True
    except Exception: pass
    if apps_path is not None and (apps_path.exists() or apps_path.is_symlink()):
        try:
            config = read_private(apps_path)
            names = config.get('apps') if isinstance(config, dict) and set(config) == {'apps'} else None
            if not isinstance(names, list) or len(names) > 20 or not all(isinstance(n, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', n) for n in names): raise ValueError('Invalid app selection')
            registry = read_private(folder/'apps.json', {})
            if not isinstance(registry, dict) or not all(n in registry for n in names): raise ValueError('Unknown app selection')
            manifest['apps'] = list(dict.fromkeys(names)); probes['apps'] = True
        except Exception: pass
    if probes['browser'] or probes['windows']:
        manifest['saved_at'] = clock(); atomic(path, manifest)
    status = {'schema': 1, 'checked_at': clock(), 'saved_at': manifest.get('saved_at'),
              'browser_available': probes['browser'], 'windows_available': probes['windows'], 'apps_config_valid': probes['apps']}
    prior_status = read_private(folder/'session-agent-status.json', {})
    for key in ['restore_boot_id', 'restore_submitted_boot_id']:
        value = prior_status.get(key) if isinstance(prior_status, dict) else None
        if isinstance(value, str) and re.fullmatch('[0-9a-f-]{36}', value): status[key] = value
    if isinstance(prior_status, dict) and prior_status.get('restore_state') in ('submitted', 'unknown', 'restored'):
        status['restore_state'] = prior_status['restore_state']
    atomic(folder/'session-agent-status.json', status)
    return status


def load_tools(path):
    path = Path(path)
    if path != Path('/usr/local/lib/mola/guest_tools.py') or path.is_symlink(): raise ValueError('Unmanaged tools source')
    for entry in [path, *path.parents]:
        info = entry.stat()
        if entry.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022: raise ValueError('Untrusted tools source')
    if not path.is_file() or path.stat().st_size > 1048576: raise ValueError('Invalid tools source')
    spec = importlib.util.spec_from_file_location('mola_session_tools', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def run(tools, folder, interval, apps_path, once=False, sleep=time.sleep, restore=False):
    if type(interval) is not int or not 30 <= interval <= 300: raise ValueError('Invalid autosave interval')
    if apps_path != folder/'session-agent-config.json': raise ValueError('Unmanaged app configuration')
    fd = os.open(folder/'session-agent.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 0
        if restore: restore_first(tools, folder)
        while True:
            started = time.monotonic()
            try: capture(tools, folder, apps_path)
            except Exception: pass  # Keep prior manifest; never log private output.
            if once: return 0
            sleep(max(0, interval-(time.monotonic()-started)))
    finally: os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--apps-json', type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--restore-first', action='store_true')
    options = parser.parse_args()
    try:
        if sys.platform != 'linux': raise ValueError('Linux service required')
        folder = private_state(Path.home())
        return run(load_tools('/usr/local/lib/mola/guest_tools.py'), folder, options.interval,
                   options.apps_json or folder/'session-agent-config.json', options.once, restore=options.restore_first)
    except Exception: return 1


if __name__ == '__main__': sys.exit(main())
