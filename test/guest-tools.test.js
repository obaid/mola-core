import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { GuestTools } from '../src/guest-tools.js';

function transport(reply, capture) {
  return (bin, args, options) => {
    const child = new EventEmitter();
    child.stdout = new PassThrough(); child.stdin = new PassThrough(); child.kill = () => {};
    let input = ''; child.stdin.on('data', data => { input += data; });
    child.stdin.on('finish', () => {
      capture?.({ bin, args, options, input });
      child.stdout.write(JSON.stringify(reply)); child.emit('close', 0);
    });
    return child;
  };
}
const target = { ssh_host: '127.0.0.1', ssh_port: 2222 };
const binding = { generation: 2, boot_id: 'current' };

test('vault bridge sends secrets only on stdin and discards guest output details', async () => {
  let request;
  const guest = new GuestTools({ spawnImpl: transport({ ok: true, result: { success: true, password: 'never-return', stdout: 'private' } }, value => { request = value; }) });
  const result = await guest.run('computer', target, 'vault-inject', { action: 'browser_login', url: 'https://example.com/login', password: 'private-password', totp: '123456' }, binding);
  assert.deepEqual(result, { success: true });
  assert.doesNotMatch(JSON.stringify(request.args), /private-password|123456|example\.com/);
  assert.match(request.input, /private-password/);
  assert.equal(request.options.stdio[2], 'ignore');
});

test('private tools refuse public SSH targets, unsupported tools, and false secret success', async () => {
  let launched = false;
  const guest = new GuestTools({ spawnImpl: () => { launched = true; } });
  await assert.rejects(guest.run('computer', { ...target, ssh_host: 'example.com' }, 'capture', {}, binding), error => error.code === 'private_guest_connection_required');
  await assert.rejects(guest.run('computer', target, 'browser', { tool: 'arbitrary', arguments: {} }, binding), error => error.status === 400);
  assert.equal(launched, false);
  const bad = new GuestTools({ spawnImpl: transport({ ok: true, result: { success: false, password: 'private' } }) });
  await assert.rejects(bad.run('computer', target, 'vault-inject', { action: 'type_secret' }, binding), error => error.code === 'secret_injection_failed' && !error.message.includes('private'));
});

test('guest tool validation and durable installer/software/session failure fixtures', () => {
  const fixture = fileURLToPath(new URL('./guest-tools-fixture.py', import.meta.url));
  const source = fileURLToPath(new URL('../runtime/guest_tools.py', import.meta.url));
  const result = spawnSync('python3', [fixture, source], { encoding: 'utf8', timeout: 25_000 });
  assert.equal(result.status, 0, result.stderr + result.stdout);
  assert.match(result.stdout, /guest tool isolation fixtures passed/);
});
