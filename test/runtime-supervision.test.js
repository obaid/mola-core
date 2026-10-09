import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { Runtime } from '../src/runtime.js';

test('native child respawn keeps config port and token, backs off failed leases and cancels on stop', async () => {
  const root = mkdtempSync(join(tmpdir(), 'mola-supervision-'));
  const previous = process.env.MOLA_HOME;
  process.env.MOLA_HOME = root;
  mkdirSync(join(root, 'runtime')); writeFileSync(join(root, 'runtime', 'host.token'), 'private-fixture-token');
  const calls = [];
  const spawnImpl = (...args) => {
    const child = new EventEmitter(); child.stderr = new PassThrough(); child.exitCode = null;
    child.kill = signal => { child.signal = signal; child.exitCode = 0; child.emit('exit', 0); };
    calls.push({ args, child }); return child;
  };
  const runtime = new Runtime({ platform:'linux', qemu:'/fixture/qemu', acceleratedGraphics:false }, {port:4177, spawnImpl, restartDelayMs:5});
  runtime.healthy = async () => true;
  try {
    await runtime.start(); const original = calls[0].args;
    calls[0].child.emit('exit',75);
    await delay(20); assert.equal(calls.length,2);
    assert.deepEqual(calls[1].args,original); assert.equal(runtime.token,'private-fixture-token');
    calls[1].child.emit('error',new Error('lease still owned')); calls[1].child.emit('exit',1);
    await delay(25); assert.equal(calls.length,3,'error and exit must not create two supervisors');
    calls[2].child.emit('exit',75); runtime.stop();
    await delay(40); assert.equal(calls.length,3); assert.equal(calls[2].child.signal,'SIGTERM');
    assert.equal(runtime.restartTimer,null);
    assert.ok(original[1].includes(join(root,'runtime','config.json')));
  } finally {
    runtime.stop(); if(previous===undefined) delete process.env.MOLA_HOME; else process.env.MOLA_HOME=previous;
    rmSync(root,{recursive:true,force:true});
  }
});
