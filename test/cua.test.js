import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CuaSessions, revokeCua } from '../src/cua.js';

test('Cua MCP sessions preserve transport state and fence another actor or boot', async () => {
  const sent = [];
  const mcp = {
    closed: false,
    async send(method, params) {
      sent.push([method, params]);
      if (method === 'initialize') return { serverInfo: { name: 'cua-driver', version: '0.28.2' } };
      if (method === 'tools/list') return { tools: [{ name: 'list_windows', inputSchema: { type: 'object' } }, { name: 'set_config' }] };
      return { content: [{ type: 'text', text: 'window' }] };
    },
    notify(method) { sent.push([method]); },
    close() { this.closed = true; },
  };
  const sessions = new CuaSessions(() => mcp);
  const binding = { generation: 2, boot_id: 'current' };
  const opened = await sessions.create('machine', {}, binding, 'user:7');
  assert.deepEqual(opened.tools.map(tool => tool.name), ['list_windows']);
  const result = await sessions.call('machine', opened.id, 'user:7', binding, 'list_windows', {});
  assert.equal(result.content[0].text, 'window');
  assert.deepEqual(sent.at(-1), ['tools/call', { name: 'list_windows', arguments: {} }]);
  await assert.rejects(sessions.call('machine', opened.id, 'user:8', binding, 'list_windows', {}), error => error.status === 404);
  await assert.rejects(sessions.call('machine', opened.id, 'user:7', binding, 'set_config', {}), error => error.status === 400);
  await assert.rejects(sessions.call('machine', opened.id, 'user:7', { ...binding, boot_id: 'next' }, 'list_windows', {}), error => error.status === 409);
  assert.equal(mcp.closed, true);
});

test('host lifecycle revokes live Cua sessions', async () => {
  const mcp = {
    closed: false, notify() {}, close() { this.closed = true; },
    async send(method) {
      if (method === 'initialize') return { serverInfo: { name: 'cua-driver', version: '0.28.2' } };
      return { tools: [{ name: 'list_windows' }] };
    },
  };
  const sessions = new CuaSessions(() => mcp);
  const binding = { generation: 1, boot_id: 'boot' };
  const opened = await sessions.create('machine-2', {}, binding, 'user:7');
  revokeCua('machine-2');
  assert.equal(mcp.closed, true);
  await assert.rejects(sessions.call('machine-2', opened.id, 'user:7', binding, 'list_windows', {}), error => error.status === 404);
});
