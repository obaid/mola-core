"""Exercise production clone sanitizer with an owned fake ext4 namespace."""
import importlib.util
import pathlib
import sys
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sanitize_clone', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='mola-clone-privacy-') as temporary:
    disk = pathlib.Path(temporary)/'staging.ext4'; disk.write_bytes(b'owned-clone-only')
    entries = {path: 'source-private-identity' for path in m.REMOVE}
    entries.update({'/etc/machine-id': 'source-machine-id', '/home/dev/customer.txt': 'preserve',
                    '/home/dev/.config/customer-credentials': 'preserve-own-credentials'})
    commands = []
    def debugfs(image, command, writable=False):
        assert image == disk
        commands.append(command)
        if command.startswith('rm '): entries.pop(command[3:]); return ''
        if command.startswith('write '): entries['/etc/machine-id'] = ''; return ''
        if command.startswith('set_inode_field '): return ''
        if command == 'stat /etc/machine-id': return 'Type: regular Size: 0'
        raise AssertionError('unexpected filesystem command')
    with patch.object(m, 'fsck'), patch.object(m, 'exists', side_effect=lambda image,path: path in entries), \
         patch.object(m, 'debugfs', side_effect=debugfs):
        m.sanitize(disk)
    assert all(path not in entries for path in m.REMOVE)
    assert entries['/home/dev/customer.txt'] == 'preserve'
    assert entries['/home/dev/.config/customer-credentials'] == 'preserve-own-credentials'
    assert entries['/etc/machine-id'] == ''
    assert 'rm /etc/mola/browser-proxy.json' in commands
    assert 'rm /etc/mola/browser-proxy-binding.json' in commands
    assert 'rm /etc/systemd/system/multi-user.target.wants/mola-browser-proxy.service' in commands
print('clone removes managed proxy credentials, binding and persistence while preserving customer files')
