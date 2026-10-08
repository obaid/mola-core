import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('agent refresh replays only its staging filesystem and rejects irreparable or unverified final state', () => {
  const module = fileURLToPath(new URL('../runtime/native/refresh_guest_agent.py', import.meta.url));
  const script = `
import importlib.util, pathlib, tempfile, hashlib, json, struct, types
spec = importlib.util.spec_from_file_location('refresh', ${JSON.stringify(module)})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as temporary:
    root = pathlib.Path(temporary); image = root / 'image'; image.mkdir()
    header = bytearray(64); header[:6] = b'\\x7fELF\\x02\\x01'; struct.pack_into('<H', header, 18, 62)
    struct.pack_into('<HH', header, 54, 4, 0)
    binary = image / 'mola-guest'; binary.write_bytes(header)
    manifest = {'schema': 1, 'file': 'mola-guest', 'architecture': 'x86_64', 'sha256': hashlib.sha256(header).hexdigest()}
    (image / 'guest-agent.json').write_text(json.dumps(manifest))
    original = root / 'current.ext4'; original.write_bytes(b'customer current disk')
    stage = root / 'unpublished.ext4'
    for preen, final, succeeds in [(0, 0, True), (1, 0, True), (2, 0, True), (4, 0, False), (8, 0, False), (0, 1, False), (0, 4, False)]:
        stage.write_bytes(b'checksummed unclean snapshot'); events = []; writes = []
        def run(argv, **options):
            assert argv[0] == 'e2fsck' and pathlib.Path(argv[-1]) == stage
            events.append(argv[1]); assert options['timeout'] == 300
            if argv[1] == '-fp': stage.write_bytes(b'repaired unpublished snapshot')
            return types.SimpleNamespace(returncode=preen if argv[1] == '-fp' else final)
        def debugfs(disk, command, writable=False):
            assert disk == stage
            if writable: writes.append(command)
            if command.startswith('dump '): pathlib.Path(command.split(' "', 1)[1][:-1]).write_bytes(header)
            if command.startswith('stat /usr/local/bin/mola-guest'): return 'Type: regular Mode: 0755 User: 0 Group: 0'
            return 'Type: directory'
        m.subprocess.run = run; m.debugfs = debugfs
        try: m.refresh(image, stage, 'x86_64')
        except ValueError: assert not succeeds, (preen, final)
        else: assert succeeds, (preen, final)
        assert original.read_bytes() == b'customer current disk'
        assert binary.read_bytes() == header
        assert events[0] == '-fp'
        if preen not in (0, 1, 2): assert writes == [] and events == ['-fp']
        else: assert events == ['-fp', '-fn']
`;
  const result = spawnSync('python3', ['-c', script], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.stdout);
});
