import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CuaRollout } from '../src/cua-rollout.js';

test('old cloud disks receive one pinned Cua installer per boot after desktop readiness', async () => {
  const record = { id: 'machine', boot_id: 'boot-1', desired_state: 'running',
    last_heartbeat_at: new Date().toISOString(),
    capabilities: { shell: true, display: true, sshd: true },
    cloud: { generation: 1, boot_requested_at: new Date(Date.now() - 1000).toISOString() } };
  let calls = 0;
  const api = {
    locked: (_id, fn) => fn(), record: () => record,
    runtime: { describe: async () => ({ status: 'running' }) },
    action: async (_target, action) => {
      calls++;
      assert.equal(action.action, 'exec');
      assert.match(action.command, /base64 -d \| \/bin\/bash/);
      return { exit_code: 0, timed_out: false };
    },
  };
  const rollout = new CuaRollout(api);
  rollout.schedule({ ...record, cloud: undefined });
  rollout.schedule(record);
  rollout.schedule(record);
  await new Promise(resolve => setImmediate(resolve));
  rollout.schedule(record);
  assert.equal(calls, 1);
  record.boot_id = 'boot-2';
  rollout.schedule(record);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
});
