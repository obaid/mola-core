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
    # Independent image-layout fixture: these names must be removed even if a
    # future sanitizer accidentally omits the persistent guest key store.
    persistent_keys = [root + '/ssh_host_' + kind + '_key' + suffix
                       for root in ['/home/dev/.mola/ssh', '/home/dev/.hyperwake/ssh']
                       for kind in ['ed25519', 'rsa'] for suffix in ['', '.pub']]
    entries.update({path: 'source-persistent-host-identity' for path in persistent_keys})
    entries['/home/dev/.ssh/id_ed25519'] = 'preserve-customer-client-key'
    entries['/home/dev/.mola/ssh/customer-note.txt'] = 'preserve-customer-note'
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
    assert all(path not in entries for path in persistent_keys)
    assert entries['/home/dev/.ssh/id_ed25519'] == 'preserve-customer-client-key'
    assert entries['/home/dev/.mola/ssh/customer-note.txt'] == 'preserve-customer-note'
    assert 'rm /etc/mola/browser-proxy.json' in commands
    assert 'rm /etc/mola/browser-proxy-binding.json' in commands
    assert 'rm /etc/systemd/system/multi-user.target.wants/mola-browser-proxy.service' in commands
print('clone resets persistent platform SSH host keys and managed proxy state while preserving customer credentials')
