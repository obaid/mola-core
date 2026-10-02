"""Supervisor restart proof using a small fake QMP peer, never a VM."""
import importlib.util
import json
import pathlib
import socket
import sys
import tempfile
import threading
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('mola_host', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ID = '11111111-1111-4111-8111-111111111111'

with tempfile.TemporaryDirectory(dir='/tmp', prefix='mola-qmp-') as temp:
    root = pathlib.Path(temp).resolve()
    image = root / 'image'; image.mkdir()
    for name in ['root.ext4', 'vmlinuz-linux', 'initramfs-linux.img']: (image / name).touch()
    config = root / 'config.json'
    config.write_text(json.dumps({'architecture': 'x86_64', 'qemu': str(root / 'qemu'), 'image': str(image), 'kernel_args': 'root=/dev/vda rw', 'gpu': 'virtio-gpu-pci', 'display': 'none'}))
    with patch.object(m.platform, 'system', return_value='Linux'), patch.object(m.platform, 'machine', return_value='x86_64'), patch.object(m.os, 'access', return_value=True), patch.object(m.subprocess, 'check_output', return_value='kvm'):
        first = m.Runner(config)
        assert first.sockets == root / 'sockets', 'Linux QMP socket must survive PrivateTmp replacement'
        folder = first.folder(ID); folder.mkdir()
        data = {'id': ID, 'managed_by': 'mola-native-v1', 'ssh_port': 1234, 'vnc_port': 1235}
        m.write_json(folder / 'machine.json', data)
        m.write_json(folder / 'running.marker', {'launching': True})
        listener = socket.socket(socket.AF_UNIX); listener.bind(str(first.sockets / (ID + '.sock'))); listener.listen()
        def qmp_peer():
            for _ in range(2):
                peer, _ = listener.accept()
                with peer, peer.makefile('rwb') as stream:
                    stream.write(b'{"QMP":{}}\n'); stream.flush()
                    for reply in [{}, {'name': 'mola-' + ID}, {'status': 'running'}]:
                        json.loads(stream.readline())
                        stream.write(json.dumps({'return': reply}).encode() + b'\n'); stream.flush()
        thread = threading.Thread(target=qmp_peer, daemon=True); thread.start()
        assert first.describe(ID)['status'] == 'running'
        restarted = m.Runner(config)
        assert restarted.processes == {}, 'new supervisor has no Popen references'
        assert restarted.describe(ID)['status'] == 'running', 'new supervisor must reconnect to persistent QMP'
        thread.join(timeout=3); assert not thread.is_alive()
        listener.close(); (first.sockets / (ID + '.sock')).unlink()
        assert restarted.describe(ID)['status'] == 'unknown', 'missing QMP with launch marker is uncertain'
        # Exercise read-only Linux PID lifetime evidence, including reuse and exit.
        original_read = m.Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/proc/sys/kernel/random/boot_id': return 'same-boot'
            return original_read(path, *args, **kwargs)
        m.write_json(folder / 'running.marker', {'pid': 43210, 'boot_id': 'same-boot', 'start_ticks': '123'})
        with patch.object(m.Path, 'read_text', read):
            with patch.object(restarted, 'linux_process_identity', return_value={'pid': 43210, 'boot_id': 'same-boot', 'start_ticks': '123', 'state': 'S'}):
                assert restarted.describe(ID)['status'] == 'unknown', 'live matching PID cannot be called stopped'
            with patch.object(restarted, 'linux_process_identity', side_effect=PermissionError('denied')):
                assert restarted.describe(ID)['status'] == 'unknown', 'unreadable process cannot be called stopped'
            with patch.object(restarted, 'linux_process_identity', return_value={'pid': 43210, 'boot_id': 'same-boot', 'start_ticks': '999', 'state': 'S'}):
                assert restarted.describe(ID)['status'] == 'stopped', 'PID reuse proves original process exited'
            assert not (folder / 'running.marker').exists()
            m.write_json(folder / 'running.marker', {'pid': 43210, 'boot_id': 'same-boot', 'start_ticks': '123'})
            with patch.object(restarted, 'linux_process_identity', side_effect=FileNotFoundError()):
                assert restarted.describe(ID)['status'] == 'stopped'
            m.write_json(folder / 'running.marker', {'pid': 43210, 'boot_id': 'previous-boot', 'start_ticks': '123'})
            assert restarted.describe(ID)['status'] == 'stopped', 'host reboot proves prior process exited'
        assert m.Runner.socket_directory(root, 'Darwin').parent == pathlib.Path('/tmp')
print('durable Linux QMP restart, conservative unknown and PID lifetime evidence passed')
