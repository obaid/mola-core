import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { Registry } from '../src/state.js';
import { HostApi, hostToken, hostDescription } from '../src/host-api.js';
import { SNAPSHOT_CHUNK_ENCODED_BYTES } from '../src/host-storage.js';

const TOKEN = 'h'.repeat(40);
function setup(t) {
  const dir = mkdtempSync(join(tmpdir(), 'mola-host-api-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'machines.json');
  const machines = new Map();
  const calls = [];
  const runtime = {
    list: async () => Object.fromEntries([...machines].map(([id, value]) => [id, value.status])),
    describe: async id => { if (!machines.has(id)) throw Object.assign(new Error('missing'), { status: 404 }); return { ...machines.get(id), disk_id: id }; },
    create: async spec => { calls.push(['create', spec]); machines.set(spec.computer_id, { status: 'stopped' }); },
    startMachine: async id => { calls.push(['start', id]); machines.get(id).status = 'running'; },
    shutdown: async id => { calls.push(['shutdown', id]); machines.get(id).status = 'stopped'; },
    forceStop: async id => { calls.push(['force-stop', id]); machines.get(id).status = 'stopped'; },
    settle: async id => { if (machines.has(id)) machines.get(id).status = 'stopped'; },
    destroy: async id => { calls.push(['destroy', id]); machines.delete(id); },
  };
  const options = { runtime, publicKey: 'ssh-ed25519 test', token: TOKEN };
  let api = new HostApi({ ...options, registry: new Registry(file) });
  const send = (parts, body, method = 'POST', token = TOKEN) => api.handle({ method, headers: { authorization: `Bearer ${token}` } }, parts.split('/'), body);
  const create = { id: randomUUID(), name: 'demo', vcpus: 2, memory_mb: 4096, disk_gb: 20, image_ref: 'omarchy-agent:0.1.0', operation_id: randomUUID(), generation: 1 };
  return { file, runtime, machines, calls, create, send, get api() { return api; }, reload() { api = new HostApi({ ...options, registry: new Registry(file) }); } };
}
const conflict = error => error.status === 409;
const command = generation => ({ operation_id: randomUUID(), generation });

test('create is stopped, caller ID survives restarts, exact retry returns saved result without credentials', async t => {
  const s = setup(t);
  const response = await s.send('machines', s.create);
  assert.equal(response.status, 201);
  assert.equal(response.body.data.id, s.create.id);
  assert.equal(response.body.data.status, 'stopped');
  assert.equal(response.body.data.ready, false);
  assert.equal(s.calls.filter(([verb]) => verb === 'start').length, 0);
  assert.ok(s.calls[0][1].registration_token);
  assert.deepEqual(s.calls[0][1].authorized_keys, ['ssh-ed25519 test']);
  assert.doesNotMatch(JSON.stringify(response), /registration_token|authorized_keys|machine_token/);
  s.reload();
  s.api.imageRef = 'new-release';
  assert.deepEqual(await s.send('machines', s.create), response);
  assert.equal(s.calls.length, 1);
  await assert.rejects(s.send('machines', { ...s.create, memory_mb: 8192 }), conflict);
});

test('unknown create outcome is reconciled after restart against the same runtime ID', async t => {
  const s = setup(t);
  const create = s.runtime.create;
  let lost = true;
  s.runtime.create = async spec => {
    const persisted = JSON.parse(readFileSync(s.file, 'utf8'))[s.create.id];
    assert.ok(persisted.cloud.operations[`create:${s.create.operation_id}`].pending_at);
    await create(spec);
    if (lost) { lost = false; throw new Error('response lost'); }
  };
  await assert.rejects(s.send('machines', s.create), /response lost/);
  s.reload();
  assert.equal((await s.send('machines', s.create)).body.data.id, s.create.id);
  assert.equal(s.machines.size, 1);
  assert.equal(s.calls[0][1].registration_token, s.calls[1][1].registration_token);
});

test('generation fencing, durable start replay, and tombstone reject delayed commands', async t => {
  const s = setup(t); await s.send('machines', s.create);
  const start = command(2);
  const result = await s.send(`machines/${s.create.id}/start`, start);
  s.reload();
  assert.deepEqual(await s.send(`machines/${s.create.id}/start`, start), result);
  await assert.rejects(s.send(`machines/${s.create.id}/shutdown`, command(1)), conflict);
  const destroy = { ...command(3), delete_disk: true };
  const removed = await s.send(`machines/${s.create.id}/destroy`, destroy);
  s.reload();
  assert.deepEqual(await s.send(`machines/${s.create.id}/destroy`, destroy), removed);
  assert.equal(removed.body.data.disk_id, null);
  assert.deepEqual((await s.send('machines', {}, 'GET')).body.data, []);
  await assert.rejects(s.send(`machines/${s.create.id}/start`, command(4)), e => e.status === 404);
  await assert.rejects(s.send('machines', { ...s.create, operation_id: randomUUID(), generation: 5 }), conflict);
  assert.equal(s.calls.filter(([verb]) => verb === 'start').length, 1);
});

test('failed ambiguous mutations block overtaking commands until same operation reconciles', async t => {
  const s = setup(t); await s.send('machines', s.create);
  const start = command(2); const real = s.runtime.startMachine;
  s.runtime.startMachine = async id => { await real(id); throw new Error('response lost'); };
  await assert.rejects(s.send(`machines/${s.create.id}/start`, start));
  s.reload();
  await assert.rejects(s.send(`machines/${s.create.id}/shutdown`, command(3)), conflict);
  s.runtime.startMachine = real;
  await s.send(`machines/${s.create.id}/start`, start);
  await s.send(`machines/${s.create.id}/shutdown`, command(3));
});

test('deterministic host capacity refusal is terminal and does not block destroy', async t => {
  const s = setup(t); await s.send('machines', s.create);
  const start = command(2);
  s.runtime.startMachine = async () => {
    throw Object.assign(new Error('Native host running-computer limit reached'), { status: 422 });
  };

  const refused = await s.send(`machines/${s.create.id}/start`, start);
  assert.deepEqual(refused, {
    status: 422,
    body: { error: 'Native host running-computer limit reached' },
  });
  s.reload();
  assert.deepEqual(await s.send(`machines/${s.create.id}/start`, start), refused);
  assert.equal((await s.send(`machines/${s.create.id}`, {}, 'GET')).body.data.status, 'stopped');

  const removed = await s.send(`machines/${s.create.id}/destroy`, {
    ...command(2), delete_disk: true,
  });
  assert.equal(removed.body.data.deleted, true);
});

test('concurrent retries serialize into one runtime mutation', async t => {
  const s = setup(t);
  await Promise.all(Array.from({ length: 8 }, () => s.send('machines', s.create)));
  assert.equal(s.calls.length, 1);
});

test('local and foreign runtime IDs cannot be adopted through create', async t => {
  const s = setup(t);
  s.api.registry.records[s.create.id] = { id: s.create.id, name: 'local', created_at: new Date().toISOString() };
  s.api.registry.flush();
  await assert.rejects(s.send('machines', s.create), conflict);
  s.api.registry.remove(s.create.id);
  s.machines.set(s.create.id, { status: 'running' });
  await assert.rejects(s.send('machines', s.create), conflict);
  assert.equal(s.calls.length, 0);
});

test('host authentication is separate, opt-in, and refuses browser origins', async t => {
  const s = setup(t);
  await assert.rejects(s.send('machines', s.create, 'POST', 'local-operator'), e => e.status === 401);
  await assert.rejects(s.api.handle({ method: 'GET', headers: { authorization: `Bearer ${TOKEN}`, origin: 'https://evil.test' } }, ['machines']), e => e.status === 403);
  assert.equal(hostToken('operator', {}), null);
  assert.throws(() => hostToken(TOKEN, { MOLA_HOST_API: '1', MOLA_HOST_TOKEN: TOKEN }), /distinct/);
  assert.equal(hostToken('operator', { MOLA_HOST_API: '1', MOLA_HOST_TOKEN: TOKEN }), TOKEN);
});

test('image and input restrictions reject before reserving a machine', async t => {
  const s = setup(t);
  await assert.rejects(s.send('machines', { ...s.create, image_ref: '/tmp/untrusted' }), conflict);
  await assert.rejects(s.send('machines', { ...s.create, registration_token: 'user-token' }), e => e.status === 400);
  assert.equal(s.api.registry.all().length, 0);
});

test('readiness requires fresh current boot and all three capabilities, old boot cannot revive after restart', async t => {
  const s = setup(t); await s.send('machines', s.create);
  const id = s.create.id;
  await s.send(`machines/${id}/start`, command(2));
  const record = s.api.registry.get(id);
  Object.assign(record, { boot_id: 'first', last_heartbeat_at: new Date().toISOString(), capabilities: { shell: true, display: true, sshd: true } });
  assert.equal(hostDescription(record, { status: 'running' }).ready, true);
  record.capabilities.display = false;
  assert.equal(hostDescription(record, { status: 'running' }).ready, false);
  await s.send(`machines/${id}/shutdown`, command(3));
  await s.send(`machines/${id}/start`, command(4));
  Object.assign(record, { boot_id: 'first', last_heartbeat_at: new Date().toISOString(), capabilities: { shell: true, display: true, sshd: true } });
  assert.equal(hostDescription(record, { status: 'running' }).ready, false);
  record.boot_id = 'second';
  assert.equal(hostDescription(record, { status: 'running' }).ready, true);
  assert.equal(hostDescription(record, null).status, 'unknown');
  assert.equal(hostDescription(record, null).ready, false);
});

test('HTTP routes keep local lifecycle fenced and private API off by default', async t => {
  const s = setup(t);
  const oldHome = process.env.MOLA_HOME;
  const oldEnabled = process.env.MOLA_HOST_API;
  const oldToken = process.env.MOLA_HOST_TOKEN;
  process.env.MOLA_HOME = mkdtempSync(join(tmpdir(), 'mola-host-http-'));
  process.env.MOLA_HOST_API = '1';
  process.env.MOLA_HOST_TOKEN = TOKEN;
  const home = process.env.MOLA_HOME;
  const { createServer } = await import('../src/server.js');
  s.runtime.start = async () => {};
  s.runtime.healthy = async () => true;
  const { server, token } = await createServer({ host: {}, port: 4141, runtime: s.runtime, registry: s.api.registry, keys: { publicKey: 'ssh-ed25519 test' } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    for (const [key, value] of [['MOLA_HOME', oldHome], ['MOLA_HOST_API', oldEnabled], ['MOLA_HOST_TOKEN', oldToken]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    rmSync(home, { recursive: true, force: true });
  });
  const url = `http://127.0.0.1:${server.address().port}`;
  const post = (path, body, bearer) => fetch(url + path, { method: 'POST', headers: { authorization: `Bearer ${bearer}`, 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const absent = await fetch(url + '/internal/v1/machines/' + randomUUID(), { headers: { authorization: `Bearer ${TOKEN}` } });
  assert.equal(absent.status, 404);
  assert.equal((await absent.json()).code, 'machine_not_found');
  const wrongRoute = await fetch(url + '/internal/v1/missing', { headers: { authorization: `Bearer ${TOKEN}` } });
  assert.equal(wrongRoute.status, 404);
  assert.equal((await wrongRoute.json()).code, undefined);
  assert.equal((await post('/internal/v1/machines', s.create, token)).status, 401);
  assert.equal((await post('/internal/v1/machines', s.create, TOKEN)).status, 201);
  assert.equal((await post(`/v1/machines/${s.create.id}/start`, {}, token)).status, 409);
  assert.equal((await post('/v1/machines', { name: 'local', vcpus: 2, memory_mb: 4096, disk_gb: 20 }, token)).status, 201);
  assert.equal(s.calls.filter(([verb]) => verb === 'start').length, 1, 'local API still creates and boots');
});


test('a boot generation spans create, start and stop, while subsequent boots fence older commands', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  const firstStart = command(1);
  await s.send(`machines/${id}/start`, firstStart);
  await s.send(`machines/${id}/shutdown`, command(1));
  await s.send(`machines/${id}/force-stop`, command(1));
  await assert.rejects(s.send(`machines/${id}/start`, command(1)), conflict);
  await s.send(`machines/${id}/start`, command(2));
  await assert.rejects(s.send(`machines/${id}/shutdown`, command(1)), conflict);
  const beforeReplay = s.calls.length;
  await s.send(`machines/${id}/start`, firstStart);
  assert.equal(s.calls.length, beforeReplay, 'old completed replay must not mutate');
  await s.send(`machines/${id}/destroy`, { ...command(2), delete_disk: true });
});

test('a shutdown intent before initial start cannot be overtaken in the same boot generation', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  await s.send(`machines/${id}/shutdown`, command(1));
  await assert.rejects(s.send(`machines/${id}/start`, command(1)), conflict);
  await s.send(`machines/${id}/start`, command(2));
});

test('private destroy preserves its pending intent when settle cannot establish runtime state', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  s.runtime.settle = async (_id, options) => { assert.equal(options.strict, true); throw new Error('runtime unreachable'); };
  await assert.rejects(s.send(`machines/${id}/destroy`, { ...command(1), delete_disk: true }), /unreachable/);
  assert.equal(s.api.registry.get(id).cloud.deleted, undefined);
  assert.equal(s.calls.filter(([verb]) => verb === 'destroy').length, 0);
});

test('strict runtime settle propagates transport failures and tolerates confirmed absence', async () => {
  const { Runtime } = await import('../src/runtime.js');
  const runtime = new Runtime({});
  runtime.forceStop = async () => { throw new Error('unreachable'); };
  runtime.describe = async () => ({ status: 'stopped' });
  await assert.rejects(runtime.settle(randomUUID(), { strict: true }), /unreachable/);
  runtime.forceStop = async () => {};
  runtime.describe = async () => { throw new Error('unreachable describe'); };
  await assert.rejects(runtime.settle(randomUUID(), { strict: true }), /unreachable describe/);
  runtime.forceStop = async () => { throw Object.assign(new Error('missing'), { status: 404 }); };
  runtime.describe = async () => { throw Object.assign(new Error('missing'), { status: 404 }); };
  await runtime.settle(randomUUID(), { strict: true });
});

test('private desktop tickets require current readiness and remain bound to their original boot', async t => {
  const s = setup(t); const id = s.create.id;
  s.api.desktop = (_id, _described, binding) => ({ binding });
  await s.send('machines', s.create);
  await s.send(`machines/${id}/start`, command(1));
  await assert.rejects(s.send(`machines/${id}/desktop`, {}), conflict);
  const record = s.api.registry.get(id);
  Object.assign(record, { boot_id: 'first', last_heartbeat_at: new Date().toISOString(), capabilities: { shell: true, display: true, sshd: true } });
  const ticket = await s.send(`machines/${id}/desktop`, {});
  const binding = ticket.body.data.binding;
  assert.deepEqual(binding, { generation: 1, boot_id: 'first' });
  assert.equal(await s.api.validateDesktop(id, binding), true);
  const describe = s.runtime.describe;
  s.runtime.describe = async value => {
    const result = await describe(value);
    record.boot_id = 'guest-rebooted-during-check';
    return result;
  };
  assert.equal(await s.api.validateDesktop(id, binding), false);
  s.runtime.describe = describe;
  record.boot_id = 'first';
  await s.send(`machines/${id}/shutdown`, command(1));
  assert.equal(await s.api.validateDesktop(id, binding), false);
  await s.send(`machines/${id}/start`, command(2));
  Object.assign(record, { boot_id: 'second', last_heartbeat_at: new Date().toISOString(), capabilities: { shell: true, display: true, sshd: true } });
  assert.equal(await s.api.validateDesktop(id, binding), false);
  await s.send(`machines/${id}/destroy`, { ...command(2), delete_disk: true });
  assert.equal(await s.api.validateDesktop(id, binding), false);
});

test('uncertain create and start never expose stopped release evidence, including after core reload', async t => {
  const s = setup(t); const id = s.create.id;
  const create = s.runtime.create;
  s.runtime.create = async spec => { await create(spec); throw new Error('lost create response'); };
  await assert.rejects(s.send('machines', s.create), /lost create/);
  s.reload();
  let observed = (await s.send(`machines/${id}`, {}, 'GET')).body.data;
  assert.equal(observed.status, 'starting');
  assert.equal(observed.ready, false);
  assert.equal((await s.send('machines', {}, 'GET')).body.data[0].status, 'starting');
  s.runtime.create = create;
  const created = await s.send('machines', s.create);
  assert.equal(created.body.data.status, 'stopped', 'completed create response remains stopped');
  assert.deepEqual(await s.send('machines', s.create), created);

  const start = command(1);
  const realStart = s.runtime.startMachine;
  s.runtime.startMachine = async () => { throw new Error('start still pending'); };
  await assert.rejects(s.send(`machines/${id}/start`, start), /pending/);
  s.reload();
  observed = (await s.send(`machines/${id}`, {}, 'GET')).body.data;
  assert.equal(s.machines.get(id).status, 'stopped');
  assert.equal(observed.status, 'starting');
  assert.equal(observed.ready, false);
  s.runtime.startMachine = realStart;
  const started = await s.send(`machines/${id}/start`, start);
  assert.equal(started.body.data.status, 'running');
  assert.deepEqual(await s.send(`machines/${id}/start`, start), started);
  assert.equal((await s.send(`machines/${id}`, {}, 'GET')).body.data.status, 'running');
});

test('pending stop and destroy preserve unknown until their durable operations reconcile', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  await s.send(`machines/${id}/start`, command(1));
  const shutdown = command(1);
  const realShutdown = s.runtime.shutdown;
  s.runtime.shutdown = async value => { await realShutdown(value); throw new Error('lost shutdown response'); };
  await assert.rejects(s.send(`machines/${id}/shutdown`, shutdown));
  s.reload();
  assert.equal(s.machines.get(id).status, 'stopped');
  assert.equal((await s.send(`machines/${id}`, {}, 'GET')).body.data.status, 'unknown');
  s.runtime.shutdown = realShutdown;
  assert.equal((await s.send(`machines/${id}/shutdown`, shutdown)).body.data.status, 'stopped');
  const destroy = { ...command(1), delete_disk: true };
  const realDestroy = s.runtime.destroy;
  s.runtime.destroy = async value => { await realDestroy(value); throw new Error('lost destroy response'); };
  await assert.rejects(s.send(`machines/${id}/destroy`, destroy));
  s.reload();
  const observed = (await s.send(`machines/${id}`, {}, 'GET')).body.data;
  assert.equal(observed.status, 'unknown');
  assert.equal(observed.ready, false);
  s.runtime.destroy = realDestroy;
  assert.equal((await s.send(`machines/${id}/destroy`, destroy)).body.data.deleted, true);
});

test('a status read racing a completed mutation cannot release from its earlier stopped snapshot', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  const describe = s.runtime.describe;
  let finishRead, beginRead;
  const entered = new Promise(resolve => { beginRead = resolve; });
  let first = true;
  s.runtime.describe = async value => {
    const result = await describe(value);
    if (first) { first = false; beginRead(); await new Promise(resolve => { finishRead = resolve; }); }
    return result;
  };
  const observation = s.send(`machines/${id}`, {}, 'GET');
  await entered;
  await s.send(`machines/${id}/start`, command(1));
  finishRead();
  assert.equal((await observation).body.data.status, 'unknown');
  assert.equal(s.machines.get(id).status, 'running');
});

test('snapshot intent survives lost responses and restore rotates identity exactly once', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  let failOnce = true, snapshots = 0, restores = 0, seeds = [];
  s.runtime.storageOperation = async (_id, verb, body) => {
    if (verb === 'snapshot') { snapshots++; if (failOnce) { failOnce = false; throw new Error('response lost'); } }
    if (verb === 'restore') restores++;
    return { id: body.snapshot_id, status: 'stopped' };
  };
  s.runtime.reseed = async (_id, body) => seeds.push(body);
  const snapshot = { ...command(1), snapshot_id: randomUUID() };
  await assert.rejects(s.send(`machines/${id}/snapshot`, snapshot), /response lost/);
  await assert.rejects(s.send(`machines/${id}/start`, command(2)), conflict);
  s.reload();
  assert.equal((await s.send(`machines/${id}/snapshot`, snapshot)).status, 201);
  await s.send(`machines/${id}/snapshot`, snapshot);
  assert.equal(snapshots, 2);
  const beforeToken = s.api.registry.get(id).registration_token;
  const restore = { ...command(2), snapshot_id: snapshot.snapshot_id };
  await s.send(`machines/${id}/restore`, restore);
  const afterToken = s.api.registry.get(id).registration_token;
  assert.notEqual(beforeToken, afterToken);
  assert.equal(seeds[0].registration_token, afterToken);
  s.reload();
  await s.send(`machines/${id}/restore`, restore);
  assert.equal(s.api.registry.get(id).registration_token, afterToken);
  assert.equal(restores, 1);
});

test('storage rejects running snapshots, arbitrary paths, stale generations and oversized chunks', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  await s.send(`machines/${id}/start`, command(2));
  s.runtime.storageOperation = async () => assert.fail('invalid storage request reached runtime');
  await assert.rejects(s.send(`machines/${id}/snapshot`, { ...command(2), snapshot_id: randomUUID() }), conflict);
  await assert.rejects(s.send(`machines/${id}/snapshot`, { ...command(2), snapshot_id: '../../disk' }), e => e.status === 400);
  await assert.rejects(s.send(`machines/${id}/snapshot-import`, { ...command(1), snapshot_id: randomUUID(), manifest: {} }), conflict);
  await assert.rejects(s.send(`machines/${id}/snapshot-read`, { snapshot_id: randomUUID(), offset: 0, url: 'http://localhost' }), e => e.status === 400);
  await assert.rejects(s.send(`machines/${id}/snapshot-write`, { snapshot_id: randomUUID(), offset: 0, sha256: 'a'.repeat(64), data: 'x'.repeat(SNAPSHOT_CHUNK_ENCODED_BYTES + 1) }), e => e.status === 400);
});

test('direct snapshot transfers keep signed grants out of the durable lifecycle journal', async t => {
  const s = setup(t); const id = s.create.id, snapshotId = randomUUID();
  await s.send('machines', s.create);
  const manifest = { id: snapshotId, artifact_bytes: 12, artifact_sha256: 'a'.repeat(64) };
  s.runtime.snapshotManifest = async () => manifest;
  s.runtime.storageOperation = async (_id, verb, body) => {
    assert.ok(['snapshot-import', 'snapshot-seal'].includes(verb));
    assert.equal(body.snapshot_id, snapshotId);
    return verb === 'snapshot-import' ? { offset: 0, complete: false } : manifest;
  };
  const calls = [];
  s.api.snapshotTransfer = {
    upload: async (...args) => { calls.push(['upload', ...args]); return { complete: true, uploaded_bytes: 12, parts: [] }; },
    download: async (...args) => { calls.push(['download', ...args]); return { complete: true, downloaded_bytes: 12 }; },
  };
  const grant = { version: 1, transfer_id: 'signed-secret' };
  assert.equal((await s.send(`machines/${id}/snapshot-export`, { snapshot_id: snapshotId, grant })).body.data.complete, true);
  assert.equal((await s.send(`machines/${id}/snapshot-import-direct`, { snapshot_id: snapshotId, manifest, grant })).body.data.complete, true);
  assert.equal(calls.length, 2);
  assert.doesNotMatch(readFileSync(s.file, 'utf8'), /signed-secret/);
});

test('source fencing is durable before runtime call and blocks delayed successful starts after reload', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  const start = command(2); await s.send(`machines/${id}/start`, start);
  let failOnce = true;
  s.runtime.storageOperation = async (_id, verb) => {
    assert.equal(verb, 'fence');
    assert.equal(JSON.parse(readFileSync(s.file, 'utf8'))[id].cloud.fenced, true);
    if (failOnce) { failOnce = false; throw new Error('connection lost'); }
    s.machines.get(id).status = 'stopped';
    return { id, fenced: true, status: 'stopped' };
  };
  const fence = command(3);
  await assert.rejects(s.send(`machines/${id}/fence`, fence), /connection lost/);
  s.reload();
  await assert.rejects(s.send(`machines/${id}/start`, start), conflict);
  await assert.rejects(s.send(`machines/${id}/start`, command(4)), conflict);
  assert.equal((await s.send(`machines/${id}/fence`, fence)).body.data.fenced, true);
  s.reload();
  assert.equal((await s.send(`machines/${id}`, {}, 'GET')).body.data.fenced, true);
  await assert.rejects(s.send(`machines/${id}/start`, command(10)), conflict);
});

test('explicit re-creation after deleted disk advances incarnation and rejects every stale lifecycle replay', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  const start = command(1); await s.send(`machines/${id}/start`, start);
  const destroy = { ...command(2), delete_disk: true };
  await s.send(`machines/${id}/destroy`, destroy);
  const oldToken = s.calls[0][1].registration_token;
  const recreate = { ...s.create, ...command(3), recreate: true };
  assert.equal((await s.send('machines', recreate)).body.data.status, 'stopped');
  assert.notEqual(s.api.registry.get(id).registration_token, oldToken);
  assert.equal(s.api.registry.get(id).cloud.incarnation_generation, 3);
  s.reload();
  assert.equal((await s.send('machines', recreate)).body.data.generation, 3);
  const before = s.calls.length;
  await assert.rejects(s.send('machines', s.create), conflict);
  await assert.rejects(s.send(`machines/${id}/start`, start), conflict);
  await assert.rejects(s.send(`machines/${id}/destroy`, destroy), conflict);
  await assert.rejects(s.send(`machines/${id}/shutdown`, command(2)), conflict);
  await assert.rejects(s.send(`machines/${id}/restore`, { ...command(2), snapshot_id: randomUUID() }), conflict);
  assert.equal(s.calls.length, before, 'retired operations must never touch new disk');
  await s.send(`machines/${id}/start`, command(3));
  assert.equal(s.machines.get(id).status, 'running');
});

test('re-creation refuses retained disks, live runtime remnants, uncertainty and missing explicit opt-in', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  await s.send(`machines/${id}/destroy`, { ...command(1), delete_disk: false });
  await assert.rejects(s.send('machines', { ...s.create, ...command(2), recreate: true }), conflict);
  const record = s.api.registry.get(id);
  // Model a separate disk-deleting tombstone without repeating the runtime.
  const deleted = Object.values(record.cloud.operations).find(value => value.verb === 'destroy');
  const payload = JSON.parse(deleted.fingerprint); payload.delete_disk = true;
  deleted.fingerprint = JSON.stringify(payload); deleted.result.body.data.disk_id = null;
  await assert.rejects(s.send('machines', { ...s.create, ...command(2) }), conflict);
  await assert.rejects(s.send('machines', { ...s.create, ...command(1), recreate: true }), conflict);
  s.machines.set(id, { status: 'running' });
  await assert.rejects(s.send('machines', { ...s.create, ...command(2), recreate: true }), conflict);
  s.machines.delete(id);
  s.runtime.describe = async () => { throw new Error('runtime unreachable'); };
  await assert.rejects(s.send('machines', { ...s.create, ...command(2), recreate: true }), /unreachable/);
  assert.equal(s.api.registry.get(id).cloud.deleted, true);
});

test('fenced source can reincarnate only after confirmed deletion and preserve retry identity after create response loss', async t => {
  const s = setup(t); const id = s.create.id;
  await s.send('machines', s.create);
  s.runtime.storageOperation = async () => ({ id, fenced: true, status: 'stopped' });
  await s.send(`machines/${id}/fence`, command(2));
  await assert.rejects(s.send('machines', { ...s.create, ...command(3), recreate: true }), conflict);
  await s.send(`machines/${id}/destroy`, { ...command(2), delete_disk: true });
  const recreate = { ...s.create, ...command(3), recreate: true };
  const create = s.runtime.create;
  let failOnce = true;
  s.runtime.create = async spec => { await create(spec); if (failOnce) { failOnce = false; throw new Error('reply lost'); } };
  await assert.rejects(s.send('machines', recreate), /reply lost/);
  const token = s.api.registry.get(id).registration_token;
  s.reload();
  await s.send('machines', recreate);
  assert.equal(s.api.registry.get(id).registration_token, token);
  assert.equal(s.api.registry.get(id).cloud.fenced, undefined);
  await s.send(`machines/${id}/start`, command(3));
  assert.equal(s.machines.size, 1);
});
