import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { COMPUTER_TOOLS } from '../src/guest-tools.js';
test('pinned proxy-only egress fixtures and private catalog boundary', () => {
  const result = spawnSync('python3', [fileURLToPath(new URL('./network-egress-fixture.py', import.meta.url)), fileURLToPath(new URL('../runtime/network_egress.py', import.meta.url))], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  for (const tools of Object.values(COMPUTER_TOOLS)) assert.equal(tools.has('network_probe'), false);
});

test('owned actual CONNECT TLS validates CA, hostname and certificate pin', () => {
  const result = spawnSync('python3', [fileURLToPath(new URL('./network-egress-tls-fixture.py', import.meta.url)), fileURLToPath(new URL('../runtime/network_egress.py', import.meta.url))], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});

test('private egress route requires host auth and exact generation/boot, including post-call races', async () => {
  const { HostApi } = await import('../src/host-api.js');
  const id = '019932dc-857a-7000-8000-000000000001';
  const record = { id, desired_state: 'running', boot_id: 'boot-one', last_heartbeat_at: new Date().toISOString(),
    capabilities: { shell: true, display: true, sshd: true }, cloud: { generation: 1, boot_requested_at: new Date(Date.now() - 1000).toISOString() } };
  let calls = 0, change = false;
  const api = new HostApi({ registry: { get: () => record }, runtime: { describe: async () => ({ status: 'running' }) }, token: 'private-host-key',
    guestTools: { run: async (machine, target, kind) => { assert.equal(kind, 'network-probe'); calls++; if (change) record.boot_id = 'boot-two'; return { country_code: 'US' }; } } });
  const parts = ['machines', id, 'network-probe'];
  const body = { tool: 'network_probe', arguments: { expected_generation: 1, expected_boot_id: 'boot-one' } };
  const request = { method: 'POST', headers: { authorization: 'Bearer private-host-key' } };
  await assert.rejects(api.handle({ method: 'POST', headers: {} }, parts, body), error => error.status === 401);
  await assert.rejects(api.handle(request, parts, { ...body, arguments: { ...body.arguments, expected_generation: 2 } }), error => error.status === 409);
  await assert.rejects(api.handle(request, parts, { ...body, arguments: { ...body.arguments, expected_boot_id: 'wrong' } }), error => error.code === 'network_probe_boot_mismatch');
  assert.equal(calls, 0);
  assert.equal((await api.handle(request, parts, body)).body.data.country_code, 'US');
  change = true;
  await assert.rejects(api.handle(request, parts, body), error => error.code === 'tool_boot_changed');
});
