import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('native create reconciles interrupted clone and rejects changed intent; retain destroy retries safely', () => {
  const modulePath = fileURLToPath(new URL('../runtime/native/host.py', import.meta.url));
  const script = `
import importlib.util, tempfile, pathlib, json
spec = importlib.util.spec_from_file_location('mola_host', ${JSON.stringify(modulePath)})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp)
    runner = m.Runner.__new__(m.Runner)
    runner.root = root
    runner.machines = root / 'machines'; runner.machines.mkdir()
    runner.image = root / 'image'; runner.image.mkdir()
    (runner.image / 'root.ext4').write_bytes(b'base-disk-data')
    runner.sockets = root / 'sockets'; runner.sockets.mkdir()
    runner.config = {'guest_endpoint': 'http://10.0.2.2:4141', 'max_memory_mb': 8192}
    runner.config_path = root / 'config.json'; runner.config_path.write_text(json.dumps(runner.config))
    runner.arch = 'x86_64'; runner.accel = 'kvm'; runner.processes = {}
    machine = {'computer_id': '11111111-1111-4111-8111-111111111111', 'name': 'test', 'vcpus': 2, 'memory_mb': 4096, 'disk_gb': 16, 'registration_token': 'secret', 'authorized_keys': []}
    seed = m.seed_disk
    def interrupted(*args): raise RuntimeError('interrupted copy')
    m.seed_disk = interrupted
    try: runner.create(machine)
    except RuntimeError: pass
    else: raise AssertionError('injected failure expected')
    assert runner.list() == {}, 'incomplete clone must never be exposed'
    m.seed_disk = seed
    first = runner.create(machine)
    assert first['status'] == 'stopped'
    assert runner.create(machine) == first
    try: runner.create(dict(machine, memory_mb=8192))
    except ValueError: pass
    else: raise AssertionError('changed specification accepted')
    folder = runner.folder(machine['computer_id'])
    metadata = runner.metadata(machine['computer_id'])
    metadata['retaining_disk'] = True
    m.write_json(folder / 'machine.json', metadata)
    retained = root / 'retained'; retained.mkdir()
    (folder / 'root.ext4').rename(retained / (machine['computer_id'] + '.ext4'))
    runner.destroy(machine['computer_id'], False)
    assert not folder.exists()
    assert (retained / (machine['computer_id'] + '.ext4')).exists()
    snapshots = root / 'snapshots' / machine['computer_id'] / '22222222-2222-4222-8222-222222222222'
    snapshots.mkdir(parents=True)
    (snapshots / 'upload.partial').write_bytes(b'partial')
    runner.destroy(machine['computer_id'], True)
    assert not (retained / (machine['computer_id'] + '.ext4')).exists()
    assert not snapshots.parent.exists()
`;
  const result = spawnSync('python3', ['-c', script], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.stdout);
});
