import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { WebSocket } from 'ws';
import { attachDataPlane, mintActionTicket, mintTunnelTicket, revokeDataPlane } from '../src/data-plane.js';

async function fixture(validate = async () => true, actionImpl) {
  const seen = [];
  const server = http.createServer();
  attachDataPlane(server, { runAction: actionImpl ?? (async (_target, action) => {
    seen.push(action);
    if (action.action === 'screenshot') return { mime_type: 'image/png', image_base64: Buffer.from('png').toString('base64') };
    if (action.action === 'read_file') return { size: 4, content_base64: Buffer.from('file').toString('base64') };
    return { ok: true, action: action.action };
  }) });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const binding = { validate };
  const ticket = mintActionTicket('machine-1', { id: 'machine-1', boot_id: 'boot-1' }, binding);
  const socket = new WebSocket(`ws://127.0.0.1:${server.address().port}/actions/socket?t=${ticket}`);
  const ready = once(socket, 'message');
  await once(socket, 'open');
  await ready;
  return { server, socket, seen };
}

function nextJson(socket) {
  return new Promise((resolve, reject) => {
    const handler = (data, binary) => {
      if (binary) return;
      socket.off('message', handler);
      socket.off('error', reject);
      resolve(JSON.parse(data.toString()));
    };
    socket.on('message', handler);
    socket.once('error', reject);
  });
}

function nextBinary(socket) {
  return new Promise((resolve, reject) => {
    const handler = (data, binary) => {
      if (!binary) return;
      socket.off('message', handler);
      socket.off('error', reject);
      resolve(Buffer.from(data));
    };
    socket.on('message', handler);
    socket.once('error', reject);
  });
}

function expectNoJson(socket, milliseconds) {
  return new Promise((resolve, reject) => {
    const handler = (_data, binary) => {
      if (binary) return;
      clearTimeout(timer);
      socket.off('message', handler);
      reject(new Error('Unexpected JSON message.'));
    };
    const timer = setTimeout(() => {
      socket.off('message', handler);
      resolve();
    }, milliseconds);
    socket.on('message', handler);
  });
}

async function close(f) {
  f.socket.close();
  await new Promise(resolve => f.server.close(resolve));
}

test('one action session carries 100 ordered requests without reconnecting', async () => {
  const f = await fixture();
  for (let index = 0; index < 100; index += 1) {
    const reply = nextJson(f.socket);
    f.socket.send(JSON.stringify({ id: String(index), op: 'action', action: { action: 'exec', command: `echo ${index}` } }));
    assert.equal((await reply).id, String(index));
  }
  assert.equal(f.seen.length, 100);
  await close(f);
});

test('action sessions use binary frames for screenshots and file writes', async () => {
  const f = await fixture();
  const metadata = nextJson(f.socket);
  const payload = nextBinary(f.socket);
  f.socket.send(JSON.stringify({ id: 'screen', op: 'action', binary: true, action: { action: 'screenshot' } }));
  const meta = await metadata;
  assert.equal(meta.binary.bytes, 3);
  assert.equal((await payload).toString(), 'png');

  const reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'write', op: 'action', binary_bytes: 4, action: { action: 'write_file', path: '/tmp/a' } }));
  f.socket.send(Buffer.from('file'));
  assert.equal((await reply).ok, true);
  assert.equal(Buffer.from(f.seen.at(-1).content_base64, 'base64').toString(), 'file');
  await close(f);
});

test('a malformed binary write fails that request without corrupting the session', async () => {
  const f = await fixture();
  let reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'bad', op: 'action', binary_bytes: 4, action: { action: 'write_file', path: '/tmp/a' } }));
  f.socket.send(Buffer.from('no'));
  assert.equal((await reply).error.code, 'invalid_binary');
  reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'after', op: 'action', action: { action: 'exec', command: 'true' } }));
  assert.equal((await reply).ok, true);
  await close(f);
});

test('screen subscriptions emit changed frames, coalesce unchanged frames, and resync', async () => {
  let image = 'first';
  const f = await fixture(async () => true, async () => {
    await new Promise(resolve => setTimeout(resolve, 10));
    return {
      mime_type: 'image/png',
      image_base64: Buffer.from(image).toString('base64'),
    };
  });
  let reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'sub', op: 'screen.subscribe', max_fps: 99 }));
  const subscribed = await reply;
  assert.equal(subscribed.ok, true);
  assert.equal(subscribed.max_fps, 10);
  const firstMetadata = nextJson(f.socket);
  const firstPayload = nextBinary(f.socket);
  const metadata = await firstMetadata;
  assert.equal(metadata.type, 'screen.frame');
  assert.equal((await firstPayload).toString(), 'first');

  // The timer may capture again, but an identical digest must not emit a frame.
  await expectNoJson(f.socket, 140);

  image = 'second';
  const changedMetadata = nextJson(f.socket);
  const changedPayload = nextBinary(f.socket);
  const changed = await changedMetadata;
  assert.equal(changed.type, 'screen.frame');
  assert.equal((await changedPayload).toString(), 'second');

  reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'sync', op: 'screen.resync' }));
  assert.equal((await reply).ok, true);
  const resyncMetadata = nextJson(f.socket);
  const resyncPayload = nextBinary(f.socket);
  const resynced = await resyncMetadata;
  assert.equal(resynced.type, 'screen.frame');
  assert.equal((await resyncPayload).toString(), 'second');
  await close(f);
});

test('duplicate request IDs are rejected instead of replaying an action', async () => {
  const f = await fixture();
  let reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'same', op: 'action', action: { action: 'exec', command: 'true' } }));
  assert.equal((await reply).ok, true);
  reply = nextJson(f.socket);
  f.socket.send(JSON.stringify({ id: 'same', op: 'action', action: { action: 'exec', command: 'false' } }));
  assert.equal((await reply).error.code, 'invalid_request_id');
  assert.equal(f.seen.length, 1);
  await close(f);
});

test('action sessions reject work above the bounded in-flight queue', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(async () => true, async () => {
    await gate;
    return { ok: true };
  });
  const rejected = new Promise((resolve, reject) => {
    const handler = (data, binary) => {
      if (binary) return;
      const message = JSON.parse(data.toString());
      if (message.id !== 'overflow') return;
      f.socket.off('message', handler);
      resolve(message);
    };
    f.socket.on('message', handler);
    f.socket.once('error', reject);
  });
  for (let index = 0; index < 16; index += 1) {
    f.socket.send(JSON.stringify({ id: `queued-${index}`, op: 'action', action: { action: 'exec', command: 'true' } }));
  }
  f.socket.send(JSON.stringify({ id: 'overflow', op: 'action', action: { action: 'exec', command: 'true' } }));
  assert.equal((await rejected).error.code, 'backpressure');
  release();
  await close(f);
});

test('lifecycle revocation closes a live session', async () => {
  const f = await fixture();
  const closed = once(f.socket, 'close');
  revokeDataPlane('machine-1');
  const [code] = await closed;
  assert.equal(code, 4001);
  await new Promise(resolve => f.server.close(resolve));
});

test('tunnels reject platform and privileged ports before minting access', () => {
  const target = { ssh_host: '127.0.0.1', ssh_port: 2222 };
  const binding = { validate: async () => true };
  assert.throws(() => mintTunnelTicket('machine-1', target, 22, binding), /allowed guest port/);
  assert.throws(() => mintTunnelTicket('machine-1', target, 4141, binding), /allowed guest port/);
  assert.doesNotThrow(() => mintTunnelTicket('machine-1', target, 9222, binding));
});

test('a tunnel carries binary bytes only to its machine-scoped SSH target', async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const child = new EventEmitter();
  child.stdin = input; child.stdout = output; child.kill = () => {};
  let spawnArgs;
  const server = http.createServer();
  attachDataPlane(server, { runAction: async () => ({}), spawnImpl: (_bin, args) => { spawnArgs = args; return child; } });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const ticket = mintTunnelTicket('machine-1', { id: 'machine-1', boot_id: 'boot-1', ssh_host: '127.0.0.1', ssh_port: 2222 }, 9222, { validate: async () => true });
  const socket = new WebSocket(`ws://127.0.0.1:${server.address().port}/tunnel/socket?t=${ticket}`);
  await once(socket, 'open');
  const received = once(input, 'data'); socket.send(Buffer.from('request')); assert.equal((await received)[0].toString(), 'request');
  const response = nextBinary(socket); output.write(Buffer.from('response')); assert.equal((await response).toString(), 'response');
  assert.ok(spawnArgs.includes('-W'));
  assert.ok(spawnArgs.includes('127.0.0.1:9222'));
  assert.equal(spawnArgs.some(value => String(value).includes('example.com')), false);
  socket.close(); await new Promise(resolve => server.close(resolve));
});
