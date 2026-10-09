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
m.SNAPSHOT_WRITE_REQUEST_BYTES = (2 * m.SNAPSHOT_CHUNK_ENCODED_BYTES) + 64 * 1024
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
    rejects(lambda: target.import_snapshot(ID, dict(payload, manifest=dict(manifest, image_ref='ubuntu-xfce:24.04-1'))), 'cross-image snapshot accepted')
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

# Durable operation requests return promptly; lost acknowledgements, duplicate
# requests, and runtime restarts all reconcile the original native receipt.
import copy
import time
import urllib.error

def restarted(runner):
    recovered = copy.copy(runner)
    recovered.lock = threading.RLock()
    recovered.machine_locks = {}; recovered.machine_locks_guard = threading.Lock()
    recovered.storage_guard = threading.RLock(); recovered.storage_workers = {}
    return recovered

def settled(runner, verb, operation_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        receipt = runner.storage_receipt(ID, verb, operation_id)
        if receipt['status'] != 'pending': return receipt
        time.sleep(0.01)
    raise AssertionError('storage operation did not settle')

with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'durable')
    runner.lock = threading.RLock()
    other_id = '33333333-3333-4333-8333-333333333333'
    other_folder = runner.folder(other_id); other_folder.mkdir()
    m.write_json(other_folder / 'machine.json', dict(runner.metadata(ID), id=other_id))
    entered, release = threading.Event(), threading.Event()
    calls = []
    original_storage = runner.storage_operation
    def delayed(identifier, verb, payload):
        calls.append((identifier, verb))
        entered.set(); assert release.wait(5)
        return original_storage(identifier, verb, payload)
    runner.storage_operation = delayed
    runner.start = lambda identifier: {'id': identifier, 'status': 'starting'}
    server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
    server.runner = runner; server.token = 'test-token'
    server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
    def http(path, body=None):
        request = urllib.request.Request('http://127.0.0.1:' + str(server.server_address[1]) + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': 'Bearer test-token'})
        try:
            with urllib.request.urlopen(request, timeout=1) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as error: return error.code, json.load(error)
    body = {'operation_id': 'slow-snapshot', 'generation': 1, 'snapshot_id': SNAPSHOT}
    path = '/machines/' + ID
    try:
        code, pending = http(path + '/snapshot', body)
        assert code == 202 and pending['storage_operation']['status'] == 'pending'
        assert entered.wait(1)
        # Pretend the first HTTP response was lost. Retrying is a receipt read,
        # and an unrelated machine can mutate while this worker is blocked.
        assert http(path + '/snapshot', body)[0] == 202
        assert http(path + '/storage-operations/snapshot/slow-snapshot')[0] == 202
        assert http('/health')[0] == 200
        assert http('/machines/' + other_id + '/start', {})[0] == 200
        assert http(path + '/start', {})[0] == 409
        assert http(path + '/snapshot', dict(body, generation=2))[0] == 409
        assert http(path + '/snapshot', dict(body, operation_id='different-operation', generation=2))[0] == 409
        release.set()
        receipt = settled(runner, 'snapshot', body['operation_id'])
        assert receipt['status'] == 'completed'
        assert len(calls) == 1, 'duplicate request performed expensive snapshot again'
        runner = restarted(runner); server.runner = runner
        assert http(path + '/snapshot', body)[1]['storage_operation'] == receipt
        assert len(calls) == 1
        assert http(path + '/snapshot', dict(body, operation_id='stale', generation=0))[0] == 422
    finally:
        release.set(); server.shutdown(); server.server_close(); server_thread.join()

# A pending journal survives termination before worker dispatch and resumes on
# restart without accepting an overtaking generation.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'dispatch-crash')
    runner.launch_storage_worker = lambda identifier, path: None
    body = {'operation_id': 'queued-snapshot', 'generation': 2, 'snapshot_id': SNAPSHOT}
    assert runner.submit_storage(ID, 'snapshot', body)['status'] == 'pending'
    rejects(lambda: runner.submit_storage(ID, 'snapshot', dict(body, generation=3, operation_id='overtake')), 'pending storage accepted an overtaking generation')
    recovered = restarted(runner)
    del recovered.launch_storage_worker
    recovered.recover_storage_operations()
    receipt = settled(recovered, 'snapshot', body['operation_id'])
    assert receipt['status'] == 'completed'
    rejects(lambda: recovered.submit_storage(ID, 'snapshot', dict(body, operation_id='stale', generation=1)), 'stale generation accepted after restart')

# Commit the restore disk, then crash before saving the completed receipt.
# Recovery proves the atomic swap from the prepared checksum without inflating
# the archive again (also safe when a core process loses the native response).
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'restore-crash')
    runner.snapshot(ID, {'snapshot_id': SNAPSHOT})
    original = (runner.folder(ID) / 'root.ext4').read_bytes()
    (runner.folder(ID) / 'root.ext4').write_bytes(b'newer disk contents' + b'\0' * (len(original) - len(b'newer disk contents')))
    original_save = runner.save_storage_receipt
    def crash_after_commit(path, receipt):
        if receipt['status'] == 'completed': raise SystemExit('simulated native process termination')
        original_save(path, receipt)
    runner.save_storage_receipt = crash_after_commit
    body = {'operation_id': 'restore-once', 'generation': 2, 'snapshot_id': SNAPSHOT}
    runner.submit_storage(ID, 'restore', body)
    for worker in runner.storage_workers.values(): worker.join(5); assert not worker.is_alive()
    assert runner.storage_receipt(ID, 'restore', body['operation_id'])['status'] == 'pending'
    assert (runner.folder(ID) / 'root.ext4').read_bytes() == original
    recovered = restarted(runner)
    del recovered.save_storage_receipt
    gzip_open = m.gzip.open
    def no_decompression(*args, **kwargs): raise AssertionError('restore was decompressed twice after commit')
    m.gzip.open = no_decompression
    try:
        recovered.recover_storage_operations()
        receipt = settled(recovered, 'restore', body['operation_id'])
        assert receipt['status'] == 'completed', receipt
        assert recovered.submit_storage(ID, 'restore', body) == receipt
    finally: m.gzip.open = gzip_open
    assert (recovered.folder(ID) / 'root.ext4').read_bytes() == original

# Corrupt restores are terminal receipts and cannot damage the existing disk.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'restore-failure')
    manifest = runner.snapshot(ID, {'snapshot_id': SNAPSHOT})
    disk = runner.folder(ID) / 'root.ext4'
    preserved = b'preserve this disk' + b'\0' * (disk.stat().st_size - len(b'preserve this disk'))
    disk.write_bytes(preserved)
    m.write_json(runner.storage_path(ID, SNAPSHOT) / 'manifest.json', dict(manifest, sha256='0' * 64))
    body = {'operation_id': 'bad-restore', 'generation': 1, 'snapshot_id': SNAPSHOT}
    runner.submit_storage(ID, 'restore', body)
    receipt = settled(runner, 'restore', body['operation_id'])
    assert receipt['status'] == 'failed' and receipt['error_status'] == 422
    assert disk.read_bytes() == preserved
    assert runner.submit_storage(ID, 'restore', body) == receipt
    assert not disk.with_suffix('.restore').exists()
print('durable storage receipts, delayed operations, duplicate fencing and crash reconciliation passed')

# A fresh disk at the same machine UUID cannot replay a retained receipt from
# the previous incarnation, even when a caller reuses its operation ID.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'incarnations')
    body = {'operation_id': 'same-operation-name', 'generation': 1, 'snapshot_id': SNAPSHOT}
    runner.submit_storage(ID, 'snapshot', body)
    old = settled(runner, 'snapshot', body['operation_id'])
    old_path = runner.operation_path(ID, 'snapshot', body['operation_id'])
    data = runner.metadata(ID)
    data.update(storage_incarnation='new-disk', storage_generation=2)
    m.write_json(runner.folder(ID) / 'machine.json', data)
    rejects(lambda: runner.submit_storage(ID, 'snapshot', body), 'retired generation mutated the new disk')
    # Simulate retirement of machine-local snapshots and a new disk's bytes.
    runner.delete_snapshot(ID, {'snapshot_id': SNAPSHOT})
    disk = runner.folder(ID) / 'root.ext4'
    replacement = b'a different disk' + b'\0' * (disk.stat().st_size - len(b'a different disk'))
    disk.write_bytes(replacement)
    runner.submit_storage(ID, 'snapshot', dict(body, generation=2))
    current = settled(runner, 'snapshot', body['operation_id'])
    assert current['result']['sha256'] == hashlib.sha256(replacement).hexdigest()
    assert old['result']['sha256'] != current['result']['sha256']
    assert old_path.exists(), 'historical receipt must remain fenced evidence'
    assert old_path != runner.operation_path(ID, 'snapshot', body['operation_id'])
    runner.recover_storage_operations()
    assert disk.read_bytes() == replacement

# Native ownership is held by an OS lease, not only in-process thread locks.
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp)
    lease = m.runtime_lease(root)
    rejects(lambda: m.runtime_lease(root), 'two native supervisors acquired the same state root')
    lease.close()
    next_lease = m.runtime_lease(root); next_lease.close()

# Exercise termination of a real worker process after atomic disk replacement,
# followed by journal recovery in a completely fresh Python process.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'process-restart')
    runner.snapshot(ID, {'snapshot_id': SNAPSHOT})
    original = (runner.folder(ID) / 'root.ext4').read_bytes()
    (runner.folder(ID) / 'root.ext4').write_bytes(b'x' * len(original))
    script = '''
import importlib.util, pathlib, sys, threading, time, os
spec = importlib.util.spec_from_file_location('mola_host', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
r = m.Runner.__new__(m.Runner)
r.root = pathlib.Path(sys.argv[2]); r.machines = r.root / 'machines'; r.sockets = r.root / 'sockets'
r.config = {}; r.arch = 'x86_64'; r.accel = 'kvm'; r.processes = {}; r.lock = threading.RLock()
lease = m.runtime_lease(r.root)
identifier = '11111111-1111-4111-8111-111111111111'
body = {'operation_id': 'real-process-crash', 'generation': 1, 'snapshot_id': '22222222-2222-4222-8222-222222222222'}
if sys.argv[3] == 'crash':
    save = r.save_storage_receipt
    def crash(path, receipt):
        if receipt['status'] == 'completed': os._exit(0)
        save(path, receipt)
    r.save_storage_receipt = crash
    r.submit_storage(identifier, 'restore', body)
else:
    def reject(*args, **kwargs): raise AssertionError('fresh process repeated archive decompression')
    m.gzip.open = reject
    r.recover_storage_operations()
for attempt in range(500):
    receipt = r.storage_receipt(identifier, 'restore', body['operation_id'])
    if receipt['status'] != 'pending':
        assert sys.argv[3] == 'recover' and receipt['status'] == 'completed', receipt
        break
    time.sleep(0.01)
else: raise AssertionError('worker did not finish')
'''
    for mode in ('crash', 'recover'):
        result = subprocess.run([sys.executable, '-c', script, sys.argv[1], str(runner.root), mode], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr + result.stdout
    assert (runner.folder(ID) / 'root.ext4').read_bytes() == original
print('incarnation fencing, supervisor lease and real worker-process crash recovery passed')

# Restore's final prepared-disk checksum can be slow too. It must not hold the
# receipt guard and stall polling or another machine's lifecycle admission.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'slow-final-checksum')
    runner.snapshot(ID, {'snapshot_id': SNAPSHOT})
    other_id = '33333333-3333-4333-8333-333333333333'
    folder = runner.folder(other_id); folder.mkdir()
    m.write_json(folder / 'machine.json', dict(runner.metadata(ID), id=other_id))
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    digest = runner.file_digest
    def slow_digest(path):
        if path.suffix == '.restore':
            entered.set(); assert release.wait(5)
        return digest(path)
    runner.file_digest = slow_digest
    body = {'operation_id': 'slow-preparation-checksum', 'generation': 1, 'snapshot_id': SNAPSHOT}
    runner.submit_storage(ID, 'restore', body)
    assert entered.wait(2)
    def observe():
        assert runner.storage_receipt(ID, 'restore', body['operation_id'])['status'] == 'pending'
        runner.require_no_pending_storage(other_id)
        observed.set()
    reader = threading.Thread(target=observe, daemon=True); reader.start()
    try:
        assert observed.wait(1), 'final disk checksum blocked receipt or unrelated machine admission'
    finally:
        release.set(); reader.join(2)
        for worker in runner.storage_workers.values(): worker.join(5)
    assert settled(runner, 'restore', body['operation_id'])['status'] == 'completed'
print('prepared restore checksums preserve independent receipt and mutation responsiveness')

# POSIX transforms retain the supervisor's open-file lease after it terminates.
if m.platform.system() != 'Windows':
    with tempfile.TemporaryDirectory() as temp:
        runner = m.Runner.__new__(m.Runner)
        root = pathlib.Path(temp); runner.lease = m.runtime_lease(root)
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                                 stdin=subprocess.PIPE, **runner.storage_subprocess_options())
        try:
            runner.lease.close()
            rejects(lambda: m.runtime_lease(root), 'orphan disk transform lost its supervisor lease')
        finally:
            child.stdin.close(); child.wait(timeout=5)
        lease = m.runtime_lease(root); lease.close()
print('POSIX disk-transform lease remains held through supervisor termination')

# Publication receipts must follow directory durability for every newly created
# snapshot namespace component; deletion receipts must follow surviving-parent
# durability, even though the deleted directory itself can no longer be synced.
with tempfile.TemporaryDirectory() as temp:
    runner = fixture(pathlib.Path(temp) / 'directory-durability')
    events = []
    synchronize = runner.sync_directory
    def sync_record(path):
        events.append(('directory', path, runner.storage_path(ID, SNAPSHOT).exists()))
        synchronize(path)
    runner.sync_directory = sync_record
    save = runner.save_storage_receipt
    def save_record(path, receipt):
        if receipt['status'] == 'completed': events.append(('completed', receipt['verb'], None))
        save(path, receipt)
    runner.save_storage_receipt = save_record
    def assert_publication(verb, body):
        events.clear(); runner.submit_storage(ID, verb, body)
        assert settled(runner, verb, body['operation_id'])['status'] == 'completed'
        completion = next(index for index, event in enumerate(events) if event[:2] == ('completed', verb))
        parents = (runner.storage_path(ID, SNAPSHOT).parent, runner.root / 'snapshots', runner.root)
        for parent in parents:
            assert any(event[:2] == ('directory', parent) for event in events[:completion]), 'missing durable namespace parent: ' + str(parent)
    body = {'snapshot_id': SNAPSHOT, 'generation': 1, 'operation_id': 'durable-snapshot'}
    assert_publication('snapshot', body)
    manifest = runner.snapshot_manifest(ID, SNAPSHOT)
    events.clear()
    deletion = dict(body, operation_id='durable-delete')
    runner.submit_storage(ID, 'snapshot-delete', deletion)
    assert settled(runner, 'snapshot-delete', deletion['operation_id'])['status'] == 'completed'
    completion = next(index for index, event in enumerate(events) if event[:2] == ('completed', 'snapshot-delete'))
    assert any(event == ('directory', runner.storage_path(ID, SNAPSHOT).parent, False) for event in events[:completion]), 'deletion was acknowledged before parent fsync'
    assert_publication('snapshot-import', dict(body, operation_id='durable-import', manifest=manifest))
    assert (runner.storage_path(ID, SNAPSHOT) / 'import.json').exists()
    assert (runner.storage_path(ID, SNAPSHOT) / 'upload.partial').exists()
print('snapshot publication, import and deletion receipts follow directory fsync ordering')

# Each disk helper propagates the supervisor lease to a nested filesystem
# writer. Kill the helper while that writer remains alive; a replacement native
# supervisor must stay excluded until the writer exits.
if m.platform.system() != 'Windows':
    import signal
    for helper in ('refresh_guest_agent', 'sanitize_clone'):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            tools = root / 'tools'; tools.mkdir()
            ready, release = root / 'writer-ready', root / 'writer-release'
            disk = root / 'staging.ext4'; disk.write_bytes(b'staging-only')
            executable = tools / 'debugfs'
            executable.write_text('#!' + sys.executable + '\n' + '''
import os, pathlib, sys, time
ready = pathlib.Path(os.environ['WRITER_READY'])
release = pathlib.Path(os.environ['WRITER_RELEASE'])
ready.write_text(str(os.getpid()))
while not release.exists(): time.sleep(0.01)
pathlib.Path(sys.argv[-1]).write_bytes(b'finished staging write')
''')
            executable.chmod(0o700)
            runner = m.Runner.__new__(m.Runner); runner.lease = m.runtime_lease(root)
            options = runner.storage_subprocess_options()
            options['env'].update(PATH=str(tools) + os.pathsep + os.environ.get('PATH', ''), WRITER_READY=str(ready), WRITER_RELEASE=str(release))
            script = 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import ' + helper + ' as helper; helper.debugfs(Path(sys.argv[2]), "rm /platform-identity", writable=True)'
            child = subprocess.Popen([sys.executable, '-c', script, str(pathlib.Path(sys.argv[1]).resolve().parent), str(disk)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)
                assert ready.exists(), 'nested writer was not started'
                child.kill(); child.wait(timeout=3)
                runner.lease.close()
                rejects(lambda: m.runtime_lease(root), 'nested writer lost the runtime lease after helper was killed')
                release.touch()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try: replacement = m.runtime_lease(root)
                    except ValueError: time.sleep(0.01); continue
                    replacement.close(); break
                else: raise AssertionError('writer did not release runtime lease')
                assert disk.read_bytes() == b'finished staging write'
            finally:
                release.touch()
                if child.poll() is None: child.kill(); child.wait(timeout=3)
                if not runner.lease.closed: runner.lease.close()
print('nested disk writers retain the POSIX lease when refresh or sanitizer helper is killed')

# A helper timeout must not create a failed terminal receipt and admit another
# restore in the SAME supervisor while a surviving grandchild still writes.
# The native worker fails closed by exiting, keeps its accepted receipt pending,
# and inherited leases also exclude replacement supervisors until writer exit.
if m.platform.system() != 'Windows':
    with tempfile.TemporaryDirectory() as temp:
        runner = fixture(pathlib.Path(temp) / 'supervisor-timeout')
        root = runner.root
        tools = root / 'tools'; tools.mkdir()
        ready, release, port_file = root / 'writer-ready', root / 'writer-release', root / 'server-port'
        staging = root / 'staging-only.ext4'; staging.write_bytes(b'unfinished preparation')
        executable = tools / 'debugfs'
        executable.write_text('#!' + sys.executable + '\n' + '''
import os, pathlib, sys, time
pathlib.Path(os.environ['WRITER_READY']).write_text(str(os.getpid()))
while not pathlib.Path(os.environ['WRITER_RELEASE']).exists(): time.sleep(0.01)
pathlib.Path(sys.argv[-1]).write_bytes(b'finished staging write')
''')
        executable.chmod(0o700)
        script = '''
import importlib.util, pathlib, sys, threading, subprocess, os, time
spec = importlib.util.spec_from_file_location('mola_host', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
r = m.Runner.__new__(m.Runner)
r.root = pathlib.Path(sys.argv[2]); r.machines = r.root / 'machines'; r.sockets = r.root / 'sockets'
r.config = {}; r.arch = 'x86_64'; r.accel = 'kvm'; r.processes = {}; r.lock = threading.RLock()
r.lease = m.runtime_lease(r.root)
def transform(identifier, verb, payload):
    options = r.storage_subprocess_options()
    options['env'].update(PATH=str(r.root / 'tools') + os.pathsep + os.environ.get('PATH', ''),
                          WRITER_READY=str(r.root / 'writer-ready'), WRITER_RELEASE=str(r.root / 'writer-release'))
    helper = 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import sanitize_clone; sanitize_clone.debugfs(Path(sys.argv[2]), "rm /identity", writable=True)'
    child = subprocess.Popen([sys.executable, '-c', helper, str(pathlib.Path(sys.argv[1]).resolve().parent), str(r.root / 'staging-only.ext4')],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    # Establish that the nested writer actually owns the inherited lease before
    # starting the timeout. Interpreter startup under parallel suites is not the
    # behavior this regression is intended to measure.
    deadline = time.monotonic() + 10
    while not (r.root / 'writer-ready').exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not (r.root / 'writer-ready').exists():
        child.kill(); child.wait()
        raise AssertionError('nested writer never acknowledged startup')
    try:
        child.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        # Match subprocess.run's helper-only timeout kill. Its nested writer
        # must survive with the lease, forcing supervisor exit/pending receipt.
        child.kill(); child.wait()
        raise
    raise AssertionError('test transformation unexpectedly finished')
r.storage_operation = transform
server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
server.runner = r; server.token = 'test-token'; server.daemon_threads = True
(r.root / 'server-port').write_text(str(server.server_address[1]))
server.serve_forever()
'''
        supervisor = subprocess.Popen([sys.executable, '-c', script, sys.argv[1], str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            while not port_file.exists() and time.monotonic() < deadline: time.sleep(0.01)
            assert port_file.exists()
            url = 'http://127.0.0.1:' + port_file.read_text() + '/machines/' + ID + '/restore'
            body = {'operation_id': 'timeout-restore', 'generation': 1, 'snapshot_id': SNAPSHOT}
            request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Authorization': 'Bearer test-token'})
            with urllib.request.urlopen(request, timeout=1) as response: assert response.status == 202
            deadline = time.monotonic() + 10
            while not ready.exists() and supervisor.poll() is None and time.monotonic() < deadline: time.sleep(0.01)
            assert ready.exists(), 'nested writer never acknowledged startup'
            assert supervisor.wait(timeout=3) == 75, 'unsafe timeout kept supervisor admitting disk mutations'
            assert ready.exists(), 'nested writer was not alive at timeout'
            receipt = json.loads(runner.operation_path(ID, 'restore', body['operation_id']).read_text())
            assert receipt['status'] == 'pending', 'timeout incorrectly settled ambiguous disk work'
            request = urllib.request.Request(url, data=json.dumps(dict(body, operation_id='second-restore', generation=2)).encode(),
                                             headers={'Authorization': 'Bearer test-token'})
            try: urllib.request.urlopen(request, timeout=1)
            except urllib.error.URLError: pass
            else: raise AssertionError('same supervisor admitted a second restore with orphan writer alive')
            assert not runner.operation_path(ID, 'restore', 'second-restore').exists()
            rejects(lambda: m.runtime_lease(root), 'replacement overlapped timed-out nested writer')
            release.touch()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try: lease = m.runtime_lease(root)
                except ValueError: time.sleep(0.01); continue
                lease.close(); break
            else: raise AssertionError('writer failed to release its lease')
        finally:
            release.touch()
            if supervisor.poll() is None: supervisor.kill(); supervisor.wait(timeout=3)
print('transform timeout preserves pending receipt and prevents same or replacement supervisor overlap')
