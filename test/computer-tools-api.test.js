import { test } from 'node:test';
import assert from 'node:assert/strict';
import { HostApi } from '../src/host-api.js';
const request = { method: 'POST', headers: { authorization: 'Bearer private-host-token' } };
function fixture() {
  const record = { id: '11111111-1111-4111-8111-111111111111', name: 'fixture', boot_id: 'boot', desired_state: 'running',
    last_heartbeat_at: new Date().toISOString(), capabilities: { shell: true, display: true, sshd: true },
    cloud: { generation: 2, operations: {}, boot_requested_at: new Date(Date.now() - 1000).toISOString() } };
  const calls = [];
  const api = new HostApi({ registry: { get: () => record, flush() {} }, runtime: { describe: async () => ({ status: 'running' }), capabilities: async () => ({running_checkpoint:false}) },
    token: 'private-host-token', publicKey: 'fixture', guestTools: { run: async (...args) => { calls.push(args); return { image_base64: 'cropped', geometry: { width: 10, height: 10 } }; } } });
  return { record, api, calls };
}
test('capture and viewer input reject stale generation before sending any guest request', async () => {
  const { api, calls } = fixture();
  await assert.rejects(api.handle(request, ['machines', 'computer', 'capture'], { expected_generation: 1, mode: 'window' }), error => error.code === 'viewer_generation_mismatch');
  await assert.rejects(api.handle(request, ['machines', 'computer', 'computer-tools'], { tool: 'viewer_input', arguments: { scope: { mode: 'desktop' }, input: { action: 'click' } } }), error => error.status === 409);
  assert.equal(calls.length, 0);
  const result = await api.handle(request, ['machines', 'computer', 'capture'], { expected_generation: 2, mode: 'window', window_id: '0x1' });
  assert.equal(result.body.data.image_base64, 'cropped');
  assert.deepEqual(calls[0].at(-1), { generation: 2, boot_id: 'boot' });
});
test('a boot change during capture discards pixels and returns a generation error', async () => {
  const { api, record } = fixture();
  api.guestTools.run = async () => { record.boot_id = 'other-boot'; return { image_base64: 'must-not-return' }; };
  await assert.rejects(api.handle(request, ['machines', 'computer', 'capture'], { expected_generation: 2, mode: 'desktop' }), error => error.code === 'tool_boot_changed');
});
test('cloud display resizing accepts the current generation and persists only confirmed dimensions', async () => {
  const { api, record, calls } = fixture();
  record.cloud.create_spec = { image_ref: 'ubuntu-xfce:24.04-4' };
  api.guestTools.run = async (...args) => { calls.push(args); return { width: 1280, height: 720 }; };
  const result = await api.handle(request, ['machines', record.id, 'geometry'], { expected_generation: 2, width: 1280, height: 720 });
  assert.equal(result.status, 200);
  assert.deepEqual(record.display, { width: 1280, height: 720 });
  assert.deepEqual(record.cloud.create_spec.display, record.display);
  assert.deepEqual(calls[0].at(-1), { generation: 2, boot_id: 'boot' });
});
test('invalid and stale display changes never reach the guest', async () => {
  const { api, calls } = fixture();
  for (const body of [
    { expected_generation: 2, width: 1280 },
    { expected_generation: 2, width: 1280, height: 720, unexpected: true },
    { expected_generation: 2, width: 1280, height: 720.5 },
  ]) await assert.rejects(api.handle(request, ['machines', 'computer', 'geometry'], body), error => error.status === 400);
  await assert.rejects(api.handle(request, ['machines', 'computer', 'geometry'], { expected_generation: 1, width: 1280, height: 720 }), error => error.status === 409);
  assert.equal(calls.length, 0);
});
test('unconfirmed guest dimensions do not replace the saved display', async () => {
  const { api, record } = fixture();
  record.display = { width: 1024, height: 768 };
  record.cloud.create_spec = { display: record.display };
  api.guestTools.run = async () => ({ width: 1280, height: 800 });
  await assert.rejects(api.handle(request, ['machines', record.id, 'geometry'], { expected_generation: 2, width: 1280, height: 720 }), error => error.status === 502);
  assert.deepEqual(record.display, { width: 1024, height: 768 });
  assert.deepEqual(record.cloud.create_spec.display, record.display);
});
test('private window identity and compositor setup require the current generation before touching the guest', async () => {
  const {api,calls}=fixture();
  for (const tool of ['window_identity','window_prepare']) {
    await assert.rejects(api.handle(request,['machines','computer','computer-tools'],{tool,arguments:{window_id:'0x1',allow_compositor:true}}),error=>error.status===409);
    await assert.rejects(api.handle(request,['machines','computer','computer-tools'],{tool,arguments:{window_id:'0x1',allow_compositor:true,expected_generation:1}}),error=>error.status===409);
  }
  assert.equal(calls.length,0);
  await api.handle(request,['machines','computer','computer-tools'],{tool:'window_prepare',arguments:{window_id:'0x1',allow_compositor:true,expected_generation:2}});
  assert.equal(calls.length,1);
  assert.deepEqual(calls[0].at(-1),{generation:2,boot_id:'boot'});
});
test('unqualified running checkpoint reports capability failure without disk mutation', async () => {
  const { api, calls, record } = fixture();
  await assert.rejects(api.handle(request, ['machines', record.id, 'checkpoint'], {operation_id:'checkpoint',generation:2,snapshot_id:'22222222-2222-4222-8222-222222222222'}), error => error.status === 501 && error.code === 'running_checkpoint_unsupported');
  assert.equal(calls.length, 0);
});
test('running checkpoint preserves boot generation and requires positive release proof before completion', async () => {
  const { api, record } = fixture();
  api.runtime.capabilities = async () => ({running_checkpoint:true});
  const body={operation_id:'checkpoint',generation:2,snapshot_id:'22222222-2222-4222-8222-222222222222'};
  api.runtime.storageOperation = async () => ({storage_operation:{verb:'checkpoint',operation_id:'checkpoint',generation:2,status:'pending'}});
  assert.equal((await api.handle(request,['machines',record.id,'checkpoint'],body)).status,202);
  assert.equal(record.cloud.generation,2);
  api.runtime.storageOperation = async () => ({checkpoint_operation_id:'checkpoint',checkpoint_attempt:'33333333-3333-4333-8333-333333333333',consistency:'filesystem',running_resumed:true,filesystem_thawed:false});
  await assert.rejects(api.handle(request,['machines',record.id,'checkpoint'],body), error=>error.status===502);
  assert.equal(record.cloud.operations['checkpoint:checkpoint'].result,undefined);
});
