"""Native disk tests use tiny temporary raw files and never start QEMU."""
import importlib.util
import json
import pathlib
import tempfile
import hashlib
import sys
import os
import threading
import subprocess
import urllib.request

spec = importlib.util.spec_from_file_location('mola_host', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.SNAPSHOT_CHUNK_BYTES = 256 * 1024
m.SNAPSHOT_CHUNK_ENCODED_BYTES = 349528
m.SNAPSHOT_WRITE_REQUEST_BYTES = m.SNAPSHOT_CHUNK_ENCODED_BYTES + 64 * 1024
ID = '11111111-1111-4111-8111-111111111111'
SNAPSHOT = '22222222-2222-4222-8222-222222222222'

def fixture(root):
    runner = m.Runner.__new__(m.Runner)
    runner.root = root; root.mkdir()
    runner.machines = root / 'machines'; runner.machines.mkdir()
    runner.sockets = root / 'sockets'; runner.sockets.mkdir()
    runner.config = {'guest_endpoint': 'http://10.0.2.2:4141'}
    runner.config_path = root / 'config.json'; runner.config_path.write_text(json.dumps(runner.config))
    runner.arch = 'x86_64'; runner.accel = 'kvm'; runner.processes = {}
    folder = runner.folder(ID); folder.mkdir()
    m.write_json(folder / 'machine.json', {'id': ID, 'managed_by': 'mola-native-v1', 'vcpus': 2, 'memory_mb': 4096, 'vnc_port': 1234, 'ssh_port': 1235})
    (folder / 'root.ext4').write_bytes(b'important guest file' + os.urandom(900000) + b'\0' * 10000000)
    return runner

def rejects(callback, message):
    try: callback()
    except (ValueError, FileNotFoundError): pass
    else: raise AssertionError(message)

with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp)
    source, target = fixture(root / 'source'), fixture(root / 'target')
    payload = {'snapshot_id': SNAPSHOT}
    original = (source.folder(ID) / 'root.ext4').read_bytes()
    manifest = source.snapshot(ID, payload)
    assert manifest['sha256'] == hashlib.sha256(original).hexdigest()
    assert source.snapshot(ID, payload) == manifest
    assert manifest['artifact_bytes'] < len(original) / 10, 'sparse disk should compress'
    source.require_stopped = lambda _: (_ for _ in ()).throw(ValueError('running'))
    rejects(lambda: source.snapshot(ID, payload), 'running snapshot accepted')
    rejects(lambda: source.restore_snapshot(ID, payload), 'running restore accepted')
    del source.require_stopped
    disk = source.folder(ID) / 'root.ext4'
    disk.write_bytes(b'changed after snapshot' + b'\0' * (len(original) - len(b'changed after snapshot')))
    source.restore_snapshot(ID, payload)
    assert disk.read_bytes() == original
    corrupt = dict(manifest, sha256='0' * 64)
    m.write_json(source.storage_path(ID, SNAPSHOT) / 'manifest.json', corrupt)
    rejects(lambda: source.restore_snapshot(ID, payload), 'corrupt restore accepted')
    assert disk.read_bytes() == original, 'failed restore changed live disk'
    m.write_json(source.storage_path(ID, SNAPSHOT) / 'manifest.json', manifest)
    assert target.import_snapshot(ID, dict(payload, manifest=manifest)) == {'offset': 0, 'complete': False}
    rejects(lambda: target.seal_snapshot(ID, payload), 'incomplete artifact sealed')
    chunk = source.read_snapshot_chunk(ID, dict(payload, offset=0))
    write = dict(payload, offset=0, data=chunk['data'], sha256=chunk['sha256'])
    result = target.write_snapshot_chunk(ID, write)
    assert result['offset'] < manifest['artifact_bytes']
    rejects(lambda: target.seal_snapshot(ID, payload), 'partial artifact sealed')
    rejects(lambda: target.write_snapshot_chunk(ID, dict(write, offset=result['offset'] + 1)), 'chunk gap accepted')
    assert target.write_snapshot_chunk(ID, write) == result, 'chunk retry must be idempotent'
    rejects(lambda: target.write_snapshot_chunk(ID, dict(write, sha256='f' * 64)), 'bad chunk checksum accepted')
    assert target.import_snapshot(ID, dict(payload, manifest=manifest))['offset'] == result['offset']
    while not chunk['eof']:
        chunk = source.read_snapshot_chunk(ID, dict(payload, offset=chunk['next_offset']))
        result = target.write_snapshot_chunk(ID, dict(payload, offset=chunk['offset'], data=chunk['data'], sha256=chunk['sha256']))
    assert result['offset'] == manifest['artifact_bytes']
    assert target.seal_snapshot(ID, payload) == manifest
    assert target.seal_snapshot(ID, payload) == manifest
    target.restore_snapshot(ID, payload)
    assert (target.folder(ID) / 'root.ext4').read_bytes() == original
    target.config['guest_agent_refresh'] = True
    target.image = root / 'missing-operator-sidecar'
    before_refresh_failure = (target.folder(ID) / 'root.ext4').read_bytes()
    try: target.restore_snapshot(ID, payload)
    except subprocess.CalledProcessError: pass
    else: raise AssertionError('hosted restore accepted a missing agent sidecar')
    assert (target.folder(ID) / 'root.ext4').read_bytes() == before_refresh_failure
    target.config['guest_agent_refresh'] = False
    target.reseed(ID, {'name': 'migrated', 'registration_token': 'new-secret', 'authorized_keys': ['ssh-ed25519 example']})
    assert b'new-secret' in (target.folder(ID) / 'identity.img').read_bytes()
    rejects(lambda: target.import_snapshot(ID, dict(payload, manifest=dict(manifest, size_bytes=10**12))), 'oversized disk accepted')
    rejects(lambda: target.storage_path(ID, '../../root'), 'path traversal accepted')
    receipt = source.fence(ID, {})
    assert receipt['fenced'] is True and receipt['status'] == 'stopped'
    rejects(lambda: source.start(ID), 'fenced machine started')
    assert source.fence(ID, {}) == receipt
    target.delete_snapshot(ID, payload)
    assert target.delete_snapshot(ID, payload)['deleted'] is True
    assert (target.folder(ID) / 'root.ext4').read_bytes() == original
print('native snapshots, restore, resumable transfer, checksums, identity and fencing passed')

# The loopback runtime receives the same base64-encoded 8 MiB chunk accepted
# by the public host API. Ordinary endpoints retain the much smaller cap.
assert m.request_body_limit('/machines/' + ID + '/snapshot-write', 'POST') == m.SNAPSHOT_WRITE_REQUEST_BYTES
assert m.request_body_limit('/machines/' + ID + '/restore', 'POST') == m.DEFAULT_REQUEST_BYTES
assert m.request_body_limit('/machines/' + ID + '/snapshot-write', 'GET') == m.DEFAULT_REQUEST_BYTES

# Health remains responsive while a long disk operation owns the mutation lock.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'health')
    runner.lock = threading.RLock()
    server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
    server.token = 'fixture-only-token'
    server.runner = runner
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with runner.lock:
            request = urllib.request.Request('http://127.0.0.1:' + str(server.server_address[1]) + '/health', headers={'Authorization': 'Bearer fixture-only-token'})
            with urllib.request.urlopen(request, timeout=1) as response:
                assert json.load(response)['ready'] is True
    finally:
        server.shutdown(); server.server_close(); thread.join()

# Disk work on one VM must not block inventory or an unrelated desktop's QMP
# observation. Same-VM mutations remain serialized and reads return uncertainty.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'concurrent')
    runner.lock = threading.RLock()
    runner.machine_locks = {}; runner.machine_locks_guard = threading.Lock()
    other_id = '33333333-3333-4333-8333-333333333333'
    other_folder = runner.folder(other_id); other_folder.mkdir()
    other_data = dict(runner.metadata(ID), id=other_id)
    m.write_json(other_folder / 'machine.json', other_data)
    runner.qmp = lambda data, command: {'status': 'running'} if data['id'] == other_id else (_ for _ in ()).throw(OSError('stopped'))
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def long_storage(identifier, verb, payload):
        assert identifier == ID and verb == 'snapshot'
        entered.set()
        assert release.wait(5), 'test did not release storage operation'
        return {'id': SNAPSHOT}
    runner.storage_operation = long_storage
    runner.stop = lambda identifier, force=False: stopped.set()
    server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
    server.token = 'fixture-only-token'; server.runner = runner
    server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
    failures = []
    def request(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request('http://127.0.0.1:' + str(server.server_address[1]) + path, data=data, headers={'Authorization': 'Bearer fixture-only-token', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=2) as response: return json.load(response)
    def post(path):
        try: request(path, {})
        except Exception as error: failures.append(str(error))
    writer = threading.Thread(target=post, args=('/machines/' + ID + '/snapshot',), daemon=True)
    follower = threading.Thread(target=post, args=('/machines/' + ID + '/force-stop',), daemon=True)
    try:
        writer.start(); assert entered.wait(2)
        follower.start()
        assert request('/machines/' + other_id)['status'] == 'running'
        assert request('/machines/' + ID)['status'] == 'unknown'
        inventory = request('/machines')
        assert inventory[other_id] == 'running' and inventory[ID] == 'unknown'
        assert not stopped.is_set(), 'same-VM stop overtook snapshot'
        release.set(); writer.join(2); follower.join(2)
        assert not failures, failures
        assert stopped.is_set(), 'serialized stop never completed'
    finally:
        release.set(); server.shutdown(); server.server_close(); server_thread.join()
print('long storage preserves independent reads and same-machine mutation serialization')
