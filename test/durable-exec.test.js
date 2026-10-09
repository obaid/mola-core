import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { HostDurableExec } from '../src/durable-exec.js';
import { HostApi } from '../src/host-api.js';
import { COMPUTER_TOOLS } from '../src/guest-tools.js';

function scene(t) {
  const root = mkdtempSync(join(tmpdir(), 'mola-durable-exec-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const id = randomUUID(), operation = randomUUID(), calls = [];
  const record = { id, boot_id: 'fixture-boot-1', cloud: { generation: 1 } };
  const service = new HostDurableExec({ root });
  const request = { operation_id: operation, expected_generation: 1, expected_boot_id: record.boot_id,
    command: 'printf fake-command-secret', timeout_seconds: 900, payload: { token: 'fake-payload-secret' }, redact: ['fake-command-secret'] };
  let response = 'queued';
  const receipt = (status = response, extra = {}) => ({ operation_id: operation, generation: 1, boot_id: 'fixture-boot-1', status,
    terminal: ['completed', 'failed'].includes(status), ...(status === 'completed' ? { result: { exit_code: 0, stdout: 'safe output', stderr: '' } } : {}), ...extra });
  const api = { registry: { get: value => value === id ? record : null }, describe: async () => ({ ready: true }), runtime: { describe: async () => ({ status: 'running' }) },
    guestTools: { run: async (machine, target, kind, body) => {
      calls.push(body.tool); assert.equal(kind, 'job-exec');
      if (body.tool === 'exec_capabilities') return { supported: true, max_timeout_seconds: 900 };
      if (body.tool === 'exec_submit') {
        const accepted = JSON.parse(readFileSync(service.path(id, operation), 'utf8'));
        assert.equal(accepted.payload_digest, body.arguments.payload_digest);
        assert.doesNotMatch(JSON.stringify(accepted), /fake-command-secret|fake-payload-secret|printf/);
      }
      return receipt();
    } } };
  return { root, id, request, record, calls, service, api, receipt, set status(value) { response = value; } };
}

test('host accepts before guest execution, keys body fingerprints, and replays without a second submit', async t => {
  const s = scene(t);
  assert.equal((await s.service.handle(s.api, s.id, 'submit', s.request)).status, 'queued');
  const disk = readdirSync(s.root).filter(name => name.endsWith('.json')).map(name => readFileSync(join(s.root, name), 'utf8')).join('');
  assert.doesNotMatch(disk, /fake-command-secret|fake-payload-secret|printf/);
  const fingerprint = JSON.parse(readFileSync(s.service.path(s.id, s.request.operation_id), 'utf8')).payload_digest;
  const wrong = { ...s.request, command: 'different command', payload_digest: fingerprint };
  await assert.rejects(s.service.handle(s.api, s.id, 'submit', wrong), error => error.code === 'exec_payload_mismatch');
  s.status = 'completed';
  const result = await s.service.handle(s.api, s.id, 'submit', s.request);
  assert.equal(result.terminal, true); assert.equal(result.result.exit_code, 0);
  assert.equal(s.calls.filter(tool => tool === 'exec_submit').length, 1);
  assert.notEqual(new HostDurableExec({ root: mkdtempSync(join(s.root, 'other-')) }).digest(s.id,
    { operation_id: s.request.operation_id, generation: 1, boot_id: s.request.expected_boot_id }, s.request), fingerprint);
});

test('lost acknowledgement and missing guest receipt remain unknown and never redispatch', async t => {
  const s = scene(t);
  s.api.guestTools.run = async (machine, target, kind, body) => {
    s.calls.push(body.tool);
    if (body.tool === 'exec_capabilities') return { supported: true, max_timeout_seconds: 900 };
    throw new Error('private diagnostic never exposed');
  };
  assert.equal((await s.service.handle(s.api, s.id, 'submit', s.request)).status, 'outcome_unknown');
  const reload = new HostDurableExec({ root: s.root });
  const result = await reload.handle(s.api, s.id, 'submit', s.request);
  assert.deepEqual(result, s.receipt('outcome_unknown'));
  assert.equal(s.calls.filter(tool => tool === 'exec_submit').length, 1);
});

test('terminal receipts resolve before stopped, new-boot and deleted machine prechecks', async t => {
  const s = scene(t); s.status = 'completed';
  const original = await s.service.handle(s.api, s.id, 'submit', s.request);
  s.record.cloud.generation = 4; s.record.boot_id = 'later-boot'; s.record.cloud.deleted = true;
  s.api.describe = async () => { throw new Error('must not read current power'); };
  assert.deepEqual(await s.service.handle(s.api, s.id, 'status', s.request), original);
  assert.equal(s.calls.filter(tool => tool !== 'exec_capabilities').length, 1);
});

test('old boot without terminal evidence stays unknown without touching a new guest', async t => {
  const s = scene(t); await s.service.handle(s.api, s.id, 'submit', s.request);
  s.calls.length = 0; s.record.cloud.generation = 2; s.record.boot_id = 'new-boot';
  assert.equal((await s.service.handle(s.api, s.id, 'status', s.request)).status, 'outcome_unknown');
  assert.deepEqual(s.calls, []);
});

test('atomic initial claims admit one guest submit across independent service instances', async t => {
  const s = scene(t); const second = new HostDurableExec({ root: s.root });
  let waiting = 0, release;
  const barrier = new Promise(resolve => { release = resolve; });
  const original = s.api.guestTools.run;
  s.api.guestTools.run = async (...args) => {
    if (args[3].tool === 'exec_capabilities') { if (++waiting === 2) release(); await barrier; }
    return original(...args);
  };
  const results = await Promise.all([s.service.handle(s.api, s.id, 'submit', s.request), second.handle(s.api, s.id, 'submit', s.request)]);
  assert.ok(results.every(result => result.status === 'queued'));
  assert.equal(s.calls.filter(tool => tool === 'exec_submit').length, 1);
});

test('a late nonterminal status cannot regress the immutable terminal receipt', async t => {
  const s = scene(t); await s.service.handle(s.api, s.id, 'submit', s.request);
  let release, started;
  const waiting = new Promise(resolve => { release = resolve; });
  const begin = new Promise(resolve => { started = resolve; });
  s.api.guestTools.run = async () => { started(); await waiting; return s.receipt('running'); };
  const late = s.service.handle(s.api, s.id, 'status', s.request); await begin;
  const path = s.service.path(s.id, s.request.operation_id), record = s.service.read(path);
  s.service.retain(path, record, s.receipt('completed'));
  release();
  assert.equal((await late).status, 'completed');
  s.record.cloud.deleted = true;
  assert.equal((await s.service.handle(s.api, s.id, 'status', s.request)).status, 'completed');
});

test('malformed terminal tuples, timeout success, raw diagnostics and mismatched bindings are rejected', async t => {
  const s = scene(t);
  const invalid = [s.receipt('completed', { result: { exit_code: 1, stdout: 'private diagnostic' } }),
    s.receipt('completed', { result: { exit_code: 0, stdout: 'private diagnostic', timed_out: true } }),
    s.receipt('completed', { terminal: false }), s.receipt('completed', { generation: 5 })];
  for (const data of invalid) {
    const body = { ...s.request, operation_id: randomUUID() };
    s.api.guestTools.run = async (machine, target, kind, request) => request.tool === 'exec_capabilities'
      ? { supported: true, max_timeout_seconds: 900 } : { ...data, operation_id: body.operation_id };
    const result = await s.service.handle(s.api, s.id, 'submit', body);
    assert.equal(result.status, 'outcome_unknown'); assert.doesNotMatch(JSON.stringify(result), /private diagnostic/);
  }
});

test('private job routes require host authorization and never enter public tool catalogs', async t => {
  const s = scene(t);
  const api = new HostApi({ registry: s.api.registry, runtime: s.api.runtime, token: 'private-host-key', durableExec: s.service, guestTools: s.api.guestTools });
  api.describe = s.api.describe;
  const parts = ['machines', s.id, 'job-exec'];
  await assert.rejects(api.handle({ method: 'POST', headers: {} }, parts, s.request), error => error.status === 401);
  await assert.rejects(api.handle({ method: 'POST', headers: { authorization: 'Bearer private-host-key', origin: 'https://evil.example' } }, parts, s.request), error => error.status === 403);
  const result = await api.handle({ method: 'POST', headers: { authorization: 'Bearer private-host-key' } }, parts, s.request);
  assert.equal(result.body.data.status, 'queued');
  assert.ok(!Object.values(COMPUTER_TOOLS).some(tools => [...tools].some(tool => /^exec_/.test(tool))));
});

test('positive original terminal evidence survives a concurrent boot change', async t => {
  const s = scene(t); await s.service.handle(s.api, s.id, 'submit', s.request);
  s.api.guestTools.run = async () => {
    s.record.cloud.generation = 2; s.record.boot_id = 'later-boot'; return s.receipt('completed');
  };
  const result = await s.service.handle(s.api, s.id, 'status', s.request);
  assert.equal(result.status, 'completed');
  s.api.describe = async () => { throw new Error('must not probe a new boot'); };
  assert.deepEqual(await s.service.handle(s.api, s.id, 'status', s.request), result);
});

test('private transport accepts the same one-MiB payload boundary as CP jobs', async t => {
  const s = scene(t);
  const request = { ...s.request, payload: { data: 'x'.repeat(1048576 - 11) } };
  assert.equal(Buffer.byteLength(JSON.stringify(request.payload)), 1048576);
  assert.equal((await s.service.handle(s.api, s.id, 'submit', request)).status, 'queued');
  await assert.rejects(s.service.handle(s.api, s.id, 'submit', { ...request, operation_id: randomUUID(), payload: { data: 'x'.repeat(1048576) } }), error => error.status === 400);
});

test('unsupported guest scope yields definite failure without an exec submission', async t => {
  const s = scene(t);
  s.api.guestTools.run = async (id, target, kind, body) => {
    assert.equal(body.tool, 'exec_capabilities'); return { supported: false, max_timeout_seconds: 0 };
  };
  const result = await s.service.handle(s.api, s.id, 'submit', s.request);
  assert.equal(result.status, 'failed'); assert.equal(result.terminal, true); assert.equal(result.result.exit_code, 125);
  assert.equal(result.result.stdout, ''); assert.equal(result.result.stderr, '');
});

test('an interrupted unpublished key cannot leave an active partial fingerprint key', t => {
  const s = scene(t);
  writeFileSync(join(s.root, 'fingerprint.key.interrupted.new'), Buffer.from([1, 2]), { mode: 0o600 });
  const key = s.service.key(); assert.equal(key.length, 32);
  assert.deepEqual(s.service.key(), key);
});

test('exec transport capability is independent of storage-runtime capability failures', async t => {
  const s = scene(t);
  const api = new HostApi({ registry: s.api.registry, runtime: { capabilities: async () => { throw new Error('unavailable storage'); } }, token: 'private-host-key' });
  const result = await api.handle({ method: 'GET', headers: { authorization: 'Bearer private-host-key' } }, ['capabilities']);
  assert.equal(result.body.data.durable_exec, true); assert.equal(result.body.data.durable_exec_max_timeout_seconds, 900);
  assert.equal(result.body.data.running_checkpoint, undefined);
});

test('native durable exec fixture proves no payload journal, one launch and crash uncertainty', () => {
  const result = spawnSync('python3', [fileURLToPath(new URL('./durable-exec-fixture.py', import.meta.url)), fileURLToPath(new URL('../runtime/durable_exec.py', import.meta.url))], { encoding: 'utf8', timeout: 25_000 });
  assert.equal(result.status, 0, result.stderr + result.stdout);
});

 test('copied operation journal never reaches another execution status or cancellation', async t => {
  const s = scene(t); await s.service.handle(s.api, s.id, 'submit', s.request);
  const path = s.service.path(s.id, s.request.operation_id);
  const record = JSON.parse(readFileSync(path, 'utf8'));
  record.operation_id = randomUUID(); writeFileSync(path, JSON.stringify(record)); s.calls.length = 0;
  for (const mode of ['status', 'cancel', 'submit']) await assert.rejects(s.service.handle(s.api, s.id, mode, s.request), error => error.code === 'exec_binding_mismatch');
  await assert.rejects(s.service.existing(s.api, s.id, path, { ...record, operation_id: s.request.operation_id }), error => error.code === 'exec_payload_mismatch');
  assert.deepEqual(s.calls, []);
});
