import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import { AutomationWorker } from '../src/automation.js';

function fakeSpawner() {
  const children = [];
  const spawn = () => {
    const child = new EventEmitter();
    child.exitCode = null;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new Writable({
      write(chunk, _encoding, callback) {
        const request = JSON.parse(chunk.toString());
        queueMicrotask(() => child.stdout.write(`${JSON.stringify({
          id: request.id,
          ok: true,
          result: { command: request.action.command },
          timing: { worker_ms: 1 },
        })}\n`));
        callback();
      },
    });
    child.kill = () => { child.exitCode = 0; child.emit('close', 0); };
    children.push(child);
    return child;
  };
  return { spawn, children };
}

test('one persistent worker serves multiple sequential actions', async () => {
  const fake = fakeSpawner();
  const worker = new AutomationWorker({ spawnImpl: fake.spawn });
  assert.equal((await worker.request({ action: { command: 'one' } })).result.command, 'one');
  assert.equal((await worker.request({ action: { command: 'two' } })).result.command, 'two');
  assert.equal(fake.children.length, 1);
  worker.close();
});

test('a worker crash fails in-flight work and the next action starts a new worker', async () => {
  const fake = fakeSpawner();
  const worker = new AutomationWorker({ spawnImpl: fake.spawn });
  const first = worker.request({ action: { command: 'one' } });
  fake.children[0].emit('close', 9);
  await assert.rejects(first, /exited with code 9/);
  assert.equal((await worker.request({ action: { command: 'two' } })).result.command, 'two');
  assert.equal(fake.children.length, 2);
  worker.close();
});

test('the worker rejects unbounded queues', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  child.kill = () => {};
  const worker = new AutomationWorker({ spawnImpl: () => child, maxPending: 1 });
  const pending = worker.request({ action: { command: 'one' } }, 50);
  await assert.rejects(worker.request({ action: { command: 'two' } }), error => error.code === 'automation_overloaded');
  await assert.rejects(pending, error => error.code === 'automation_timeout');
});

test('malformed worker output fails current work and restarts cleanly', async () => {
  const fake = fakeSpawner();
  const worker = new AutomationWorker({ spawnImpl: fake.spawn });
  const pending = worker.request({ action: { command: 'one' } });
  fake.children[0].stdout.write('not-json\n');
  await assert.rejects(pending, /malformed output/);
  assert.equal((await worker.request({ action: { command: 'two' } })).result.command, 'two');
  assert.equal(fake.children.length, 2);
  worker.close();
});

test('closing a worker rejects work instead of leaving promises pending', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.stdin = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  child.kill = () => {};
  const worker = new AutomationWorker({ spawnImpl: () => child });
  const pending = worker.request({ action: { command: 'one' } });
  worker.close();
  await assert.rejects(pending, error => error.code === 'automation_closed');
});
