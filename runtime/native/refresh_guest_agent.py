"""Install only the operator-pinned guest daemon into a disposable ext4 copy.

No guest paths, download URLs or binary locations come from an API request.
The caller publishes the disk only after this helper verifies its result.
"""
import argparse
import os
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile

DESTINATION = '/usr/local/bin/mola-guest'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def validate_binary(path, architecture, expected_digest):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Guest daemon must be a regular binary smaller than 64 MiB')
    if not re.fullmatch('[0-9a-f]{64}', expected_digest) or digest(path) != expected_digest:
        raise ValueError('Trusted guest daemon checksum mismatch')
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b'\x7fELF\x02\x01': raise ValueError('Guest daemon must be a Linux 64-bit little-endian ELF binary')
        machine = struct.unpack_from('<H', header, 18)[0]
        if machine != {'x86_64': 62, 'aarch64': 183}.get(architecture): raise ValueError('Guest daemon architecture mismatch')
        offset = struct.unpack_from('<Q', header, 32)[0]
        entry_size, count = struct.unpack_from('<HH', header, 54)
        if entry_size < 4 or count > 1024 or offset + entry_size * count > path.stat().st_size: raise ValueError('Invalid ELF program headers')
        for index in range(count):
            stream.seek(offset + index * entry_size)
            if struct.unpack('<I', stream.read(4))[0] == 3: raise ValueError('Guest daemon must be statically linked')


def quote(value):
    if any(character in str(value) for character in ['\n', '\r', '\x00']): raise ValueError('Invalid debugfs path')
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
    result = subprocess.run(['debugfs', *(['-w'] if writable else []), '-R', command, str(disk)], capture_output=True, text=True, timeout=120, **storage_lease_options())
    if result.returncode != 0: raise ValueError('Guest filesystem operation failed')
    return result.stdout


def fsck(disk, repair=False):
    # A stopped snapshot may follow a forced power-off. Replay its journal and
    # preen only the unpublished, checksummed staging copy before touching the
    # agent. Codes 1 (corrected) and 2 (reboot recommended) are successful for an
    # offline disk that has not booted; every other nonzero result is refused.
    # The final check remains strictly read-only and must return zero.
    result = subprocess.run(['e2fsck', '-fp' if repair else '-fn', str(disk)], capture_output=True, text=True, timeout=300, **storage_lease_options())
    if result.returncode not in ((0, 1, 2) if repair else (0,)):
        raise ValueError('Guest filesystem failed staging repair' if repair else 'Guest filesystem failed read-only consistency validation')


def refresh(image, disk, expected_architecture=None):
    if disk.is_symlink() or not disk.is_file(): raise ValueError('Restore staging disk must be a regular file')
    manifest_path = image / 'guest-agent.json'
    if manifest_path.is_symlink() or not manifest_path.is_file(): raise ValueError('Trusted guest daemon manifest is required')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 1 or manifest.get('file') != 'mola-guest' or manifest.get('architecture') not in ['x86_64', 'aarch64']:
        raise ValueError('Invalid trusted guest daemon manifest')
    if expected_architecture is not None and manifest['architecture'] != expected_architecture: raise ValueError('Trusted guest daemon does not match runtime architecture')
    binary = image / 'mola-guest'
    validate_binary(binary, manifest['architecture'], str(manifest.get('sha256', '')))
    fsck(disk, repair=True)
    for directory in ['/usr', '/usr/local', '/usr/local/bin']:
        if not re.search(r'Type:\s+directory\b', debugfs(disk, 'stat ' + directory)):
            raise ValueError('Guest daemon parent must be a real directory')
    if not re.search(r'Type:\s+regular\b', debugfs(disk, 'stat ' + DESTINATION)):
        raise ValueError('Guest daemon destination must already be a regular file')
    debugfs(disk, 'rm ' + DESTINATION, writable=True)
    debugfs(disk, 'write ' + quote(binary) + ' ' + DESTINATION, writable=True)
    for field, value in [('mode', '0100755'), ('uid', '0'), ('gid', '0')]:
        debugfs(disk, 'set_inode_field ' + DESTINATION + ' ' + field + ' ' + value, writable=True)
    metadata = debugfs(disk, 'stat ' + DESTINATION)
    if not re.search(r'Type:\s+regular\b.*Mode:\s+0755\b', metadata) or not re.search(r'User:\s+0\s+Group:\s+0\b', metadata):
        raise ValueError('Guest daemon installation metadata mismatch')
    with tempfile.TemporaryDirectory(prefix='mola-verify-agent-') as temporary:
        extracted = Path(temporary) / 'mola-guest'
        debugfs(disk, 'dump ' + DESTINATION + ' ' + quote(extracted))
        if not extracted.is_file() or digest(extracted) != manifest['sha256']:
            raise ValueError('Guest daemon installation checksum mismatch')
    fsck(disk)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--disk', type=Path, required=True)
    parser.add_argument('--architecture', choices=['x86_64', 'aarch64'], required=True)
    args = parser.parse_args()
    try:
        refreshed = refresh(args.image, args.disk, args.architecture)
        print(json.dumps({'guest_agent_sha256': refreshed['sha256'], 'refreshed': True}))
    except Exception:
        # Never leak guest files, command output or private filesystem paths.
        raise SystemExit('Trusted guest daemon refresh failed; current disk was not published')
