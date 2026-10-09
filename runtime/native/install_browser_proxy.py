"""Install a trusted Chrome proxy before the disposable guest disk is booted."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser_proxy import validate
from refresh_guest_agent import debugfs, quote

ROOT = Path(__file__).resolve().parent.parent
POLICY = '/etc/opt/chrome/policies/managed/mola-browser-proxy.json'
CONFIG = '/etc/mola/browser-proxy.json'
BINDING = '/etc/mola/browser-proxy-binding.json'
SERVICE = '/etc/systemd/system/mola-browser-proxy.service'
ENABLED = '/etc/systemd/system/multi-user.target.wants/mola-browser-proxy.service'
HELPER = '/usr/local/bin/mola-browser-proxy'


def install(disk, config, binding=None):
    if config is not None: validate(config)
    def kind(path):
        found = re.search(r'Type:\s+(\w+)', debugfs(disk, 'stat ' + path))
        return found.group(1) if found else None
    def directory(path):
        for part in list(Path(path).parents)[::-1][1:] + [Path(path)]:
            value = kind(str(part))
            if value is None: debugfs(disk, 'mkdir ' + str(part), writable=True)
            elif value != 'directory': raise ValueError('Unsafe proxy installation directory')
    def remove(path):
        value = kind(path)
        if value not in [None, 'regular', 'symlink']: raise ValueError('Unsafe proxy installation file')
        if value is not None: debugfs(disk, 'rm ' + path, writable=True)
    def write(path, content, mode):
        directory(str(Path(path).parent))
        remove(path)
        with tempfile.NamedTemporaryFile() as temporary:
            temporary.write(content.encode()); temporary.flush()
            debugfs(disk, 'write ' + quote(temporary.name) + ' ' + path, writable=True)
        for field, value in [('mode', mode), ('uid', '0'), ('gid', '0')]:
            debugfs(disk, 'set_inode_field ' + path + ' ' + field + ' ' + value, writable=True)
        metadata = debugfs(disk, 'stat ' + path)
        if kind(path) != 'regular' or not re.search(r'User:\s+0\s+Group:\s+0', metadata) or not re.search(r'Mode:\s+' + mode[-4:] + r'\b', metadata):
            raise ValueError('Proxy installation metadata mismatch')
        with tempfile.TemporaryDirectory() as temporary:
            extracted = Path(temporary) / 'installed'
            debugfs(disk, 'dump ' + path + ' ' + quote(extracted))
            if not extracted.is_file() or hashlib.sha256(extracted.read_bytes()).digest() != hashlib.sha256(content.encode()).digest():
                raise ValueError('Proxy installation checksum mismatch')
    # A fork never inherits the source computer's proxy identity. Reapply the
    # destination's binding, or remove our managed proxy when none was chosen.
    for path in [POLICY, CONFIG, BINDING, SERVICE, ENABLED, HELPER]:
        directory(str(Path(path).parent))
        remove(path)
    if config is None: return
    write(CONFIG, json.dumps(config), '0100600')
    if binding is not None:
        if not isinstance(binding, dict) or set(binding) != {'configuration_id', 'country', 'operation_id'} or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', v) for v in binding.values()): raise ValueError('Invalid network binding')
        write(BINDING, json.dumps(binding), '0100644')
    write(HELPER, (ROOT / 'browser_proxy.py').read_text(), '0100644')
    write(POLICY, json.dumps({
        'ProxySettings': {'ProxyMode': 'fixed_servers', 'ProxyServer': 'http://127.0.0.1:18888', 'ProxyBypassList': 'localhost;127.0.0.1;[::1]'},
        'WebRtcIPHandling': 'disable_non_proxied_udp', 'QuicAllowed': False,
    }), '0100644')
    write(SERVICE, '[Unit]\nDescription=Mola browser proxy\nAfter=network-online.target\nWants=network-online.target\n'
          '[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/mola-browser-proxy\nRestart=always\nRestartSec=2\n'
          'NoNewPrivileges=true\nProtectSystem=strict\nProtectHome=true\nPrivateTmp=true\nReadWritePaths=/var/lib/mola/browser-proxy\n'
          '[Install]\nWantedBy=multi-user.target\n', '0100644')
    debugfs(disk, 'symlink ' + ENABLED + ' ' + SERVICE, writable=True)
    if kind(ENABLED) != 'symlink': raise ValueError('Proxy service was not enabled')
    directory('/var/lib/mola/browser-proxy')
