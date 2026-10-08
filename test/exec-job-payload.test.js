import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
test('structured job payload reaches only guest stdin and preserves JSON types', () => {
  const source = fileURLToPath(new URL('../runtime/automation.py', import.meta.url));
  const loader = 'import importlib.util,sys; s=importlib.util.spec_from_file_location("a",sys.argv[1]); a=importlib.util.module_from_spec(s); s.loader.exec_module(a); exec(a.GUEST_PROGRAM)';
  const command = "python3 -c 'import json,os,sys; p=json.load(sys.stdin); print(json.dumps({\"payload\":p,\"run\":os.environ[\"MOLA_JOB_RUN_ID\"]}))'";
  const payload = { nested: [1, true, null, { text: '$(this must remain data); `and this`' }] };
  const result = spawnSync('python3', ['-c', loader, source], { input: JSON.stringify({ action: 'exec', command, payload, job_run_id: 'owned-run', timeout: 5 }), encoding: 'utf8', timeout: 10_000 });
  assert.equal(result.status, 0, result.stderr);
  const response = JSON.parse(result.stdout);
  assert.equal(response.exit_code, 0);
  assert.deepEqual(JSON.parse(response.stdout), { payload, run: 'owned-run' });
  assert.equal(response.timed_out, false);
});
