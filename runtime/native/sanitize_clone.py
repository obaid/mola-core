"""Remove platform identity from an offline ext4 disk before publishing a clone.

Customer files and customer-installed credentials intentionally remain. Only
identities that would make two machines claim to be the same host are reset.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


REMOVE = [
    '/etc/ssh/ssh_host_dsa_key', '/etc/ssh/ssh_host_dsa_key.pub',
    '/etc/ssh/ssh_host_ecdsa_key', '/etc/ssh/ssh_host_ecdsa_key.pub',
    '/etc/ssh/ssh_host_ed25519_key', '/etc/ssh/ssh_host_ed25519_key.pub',
    '/etc/ssh/ssh_host_rsa_key', '/etc/ssh/ssh_host_rsa_key.pub',
    # The guest entrypoint restores /etc/ssh from this persistent platform
    # key store on every boot. Clear both stores on a fork so the entrypoint
    # generates fresh host identities; customer ~/.ssh credentials remain.
    '/home/dev/.mola/ssh/ssh_host_dsa_key', '/home/dev/.mola/ssh/ssh_host_dsa_key.pub',
    '/home/dev/.mola/ssh/ssh_host_ecdsa_key', '/home/dev/.mola/ssh/ssh_host_ecdsa_key.pub',
    '/home/dev/.mola/ssh/ssh_host_ed25519_key', '/home/dev/.mola/ssh/ssh_host_ed25519_key.pub',
    '/home/dev/.mola/ssh/ssh_host_rsa_key', '/home/dev/.mola/ssh/ssh_host_rsa_key.pub',
    '/home/dev/.hyperwake/ssh/ssh_host_dsa_key', '/home/dev/.hyperwake/ssh/ssh_host_dsa_key.pub',
    '/home/dev/.hyperwake/ssh/ssh_host_ecdsa_key', '/home/dev/.hyperwake/ssh/ssh_host_ecdsa_key.pub',
    '/home/dev/.hyperwake/ssh/ssh_host_ed25519_key', '/home/dev/.hyperwake/ssh/ssh_host_ed25519_key.pub',
    '/home/dev/.hyperwake/ssh/ssh_host_rsa_key', '/home/dev/.hyperwake/ssh/ssh_host_rsa_key.pub',
    '/var/lib/dbus/machine-id',
    '/var/lib/mola/machine-token', '/var/lib/mola/machine-id',
    '/var/lib/mola/computer-id', '/var/lib/mola/enrollment-key',
    '/var/lib/hyperwake/machine-token', '/var/lib/hyperwake/machine-id',
    '/var/lib/hyperwake/computer-id', '/var/lib/hyperwake/enrollment-key',
    # Per-computer managed egress identity must never become a fork's identity.
    # Owner snapshots preserve their guest disk bytes; only the unpublished
    # clone staging disk is stripped, before destination enrollment/reseed.
    '/etc/mola/browser-proxy.json', '/etc/mola/browser-proxy.json.new',
    '/etc/mola/browser-proxy-binding.json', '/etc/mola/browser-proxy-binding.json.new',
    '/etc/opt/chrome/policies/managed/mola-browser-proxy.json',
    '/etc/opt/chrome/policies/managed/mola-browser-proxy.json.new',
    '/etc/systemd/system/mola-browser-proxy.service',
    '/etc/systemd/system/mola-browser-proxy.service.new',
    '/etc/systemd/system/multi-user.target.wants/mola-browser-proxy.service',
    '/usr/local/bin/mola-browser-proxy', '/usr/local/bin/mola-browser-proxy.new',
    '/var/lib/mola/browser-proxy/counters.json', '/var/lib/mola/browser-proxy/counters.new',
]


def quote(value):
    if any(character in str(value) for character in ['\n', '\r', '\x00']):
        raise ValueError('Invalid filesystem path')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def storage_lease_options():
    """Keep the supervisor lease in every nested filesystem tool process."""
    value = os.environ.get('MOLA_STORAGE_LEASE_FD')
    if value is None: return {}
    if os.name == 'nt' or not value.isdigit(): raise ValueError('Invalid inherited storage lease')
    descriptor = int(value)
    os.fstat(descriptor) # Fail closed if a caller dropped the advertised lease.
    return {'pass_fds': (descriptor,)}


def debugfs(disk, command, writable=False):
    result = subprocess.run(
        ['debugfs', *(['-w'] if writable else []), '-R', command, str(disk)],
        capture_output=True, text=True, timeout=120, **storage_lease_options(),
    )
    if result.returncode != 0:
        raise ValueError('Guest filesystem operation failed')
    return result.stdout + result.stderr


def exists(disk, path):
    output = debugfs(disk, 'stat ' + path)
    return bool(re.search(r'Inode:\s+\d+', output)) and 'File not found' not in output


def fsck(disk):
    # Work only on the unpublished staging copy. `-p` safely replays a dirty
    # journal and applies repairs that need no operator decision, which lets a
    # snapshot made after an unclean guest shutdown remain usable. e2fsck uses
    # bit 1 for corrected errors and bit 2 for "reboot recommended"; neither
    # is an error for an offline image that has not been booted yet.
    result = subprocess.run(['e2fsck', '-fp', str(disk)], capture_output=True, text=True, timeout=300, **storage_lease_options())
    if result.returncode not in (0, 1, 2):
        raise ValueError('Guest filesystem failed consistency validation')


def sanitize(disk):
    if disk.is_symlink() or not disk.is_file():
        raise ValueError('Clone staging disk must be a regular file')
    fsck(disk)
    for path in REMOVE:
        if exists(disk, path):
            debugfs(disk, 'rm ' + path, writable=True)

    # systemd generates a fresh machine ID when this file exists but is empty.
    if exists(disk, '/etc/machine-id'):
        debugfs(disk, 'rm /etc/machine-id', writable=True)
    with tempfile.NamedTemporaryFile() as empty:
        debugfs(disk, 'write ' + quote(empty.name) + ' /etc/machine-id', writable=True)
    for field, value in [('mode', '0100644'), ('uid', '0'), ('gid', '0')]:
        debugfs(disk, 'set_inode_field /etc/machine-id ' + field + ' ' + value, writable=True)

    if not re.search(r'Type:\s+regular\b.*Size:\s+0\b', debugfs(disk, 'stat /etc/machine-id'), re.S):
        raise ValueError('Machine identity was not reset')
    for path in REMOVE:
        if exists(disk, path):
            raise ValueError('Clone identity file survived sanitization')
    fsck(disk)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', type=Path, required=True)
    args = parser.parse_args()
    try:
        sanitize(args.disk)
    except Exception:
        raise SystemExit('Clone identity reset failed; current disk was not published')
