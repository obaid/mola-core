import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { Runtime } from '../src/runtime.js';

async function runtimeFixture(t, handle) {
  const requests = [];
  const server = createServer((request, response) => {
    requests.push([request.method, request.url]);
    assert.equal(request.headers.authorization, 'Bearer fixture-token');
    const payload = handle(request, requests.length);
    response.writeHead(payload.storage_operation?.status === 'pending' ? 202 : 200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(payload));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const runtime = new Runtime({});
  runtime.base = `http://127.0.0.1:${server.address().port}`;
  runtime.token = 'fixture-token';
  return { runtime, requests };
}
const command = { operation_id: 'test-operation', generation: 2, snapshot_id: 'test-snapshot' };
const pending = { operation_id: command.operation_id, generation: command.generation, verb: 'restore', status: 'pending' };

test('runtime bounded wait returns pending instead of timing out a long native restore', async t => {
  const { runtime, requests } = await runtimeFixture(t, () => ({ storage_operation: pending }));
  const started = Date.now();
  const result = await runtime.storageOperation('test-machine', 'restore', command);
  assert.deepEqual(result.storage_operation, pending);
  assert.ok(Date.now() - started < 4000);
  assert.deepEqual(requests[0], ['POST', '/machines/test-machine/restore']);
  assert.ok(requests.length > 1);
  assert.ok(requests.slice(1).every(([method, path]) => method === 'GET' && path === '/machines/test-machine/storage-operations/restore/test-operation'));
});

test('runtime polling observes completion without submitting another restore', async t => {
  const complete = { ...pending, status: 'completed', result: { status: 'stopped', snapshot_sha256: 'test-digest' } };
  const { runtime, requests } = await runtimeFixture(t, (_request, count) => ({ storage_operation: count < 3 ? pending : complete }));
  assert.deepEqual((await runtime.storageOperation('test-machine', 'restore', command)).storage_operation, complete);
  assert.equal(requests.filter(([method]) => method === 'POST').length, 1);
});
