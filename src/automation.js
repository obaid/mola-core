import { createHash, randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { runtimeScript, statePath } from './paths.js';
import { pythonBin } from './python.js';

const DEFAULT_TIMEOUT = 180_000;
const DEFAULT_QUEUE = 64;

export class AutomationWorker {
  constructor({ spawnImpl = spawn, maxPending = DEFAULT_QUEUE } = {}) {
    this.spawnImpl = spawnImpl;
    this.maxPending = maxPending;
    this.child = null;
    this.buffer = '';
    this.pending = new Map();
  }

  start() {
    if (this.child && this.child.exitCode === null) return false;
    const child = this.spawnImpl(pythonBin(), [runtimeScript('automation.py'), '--worker'], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.child = child;
    this.buffer = '';
    child.stdout.on('data', chunk => this.#consume(chunk.toString()));
    child.stderr.on('data', chunk => {
      if (process.env.MOLA_AUTOMATION_DEBUG === '1') process.stderr.write(`[automation] ${chunk}`);
    });
    child.on('error', error => this.#failed(child, error));
    child.on('close', code => this.#failed(child, new Error(`Automation worker exited with code ${code}.`)));
    return true;
  }

  #consume(chunk) {
    this.buffer += chunk;
    for (;;) {
      const end = this.buffer.indexOf('\n');
      if (end < 0) return;
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      if (!line) continue;
      let message;
      try { message = JSON.parse(line); } catch { this.#failed(this.child, new Error('Automation worker returned malformed output.')); return; }
      const request = this.pending.get(message.id);
      if (!request) continue;
      this.pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.ok) request.resolve(message);
      else request.reject(Object.assign(new Error(message.error || 'Automation transport failed.'), { code: message.code }));
    }
  }

  #failed(child, error) {
    if (this.child !== child) return;
    this.child = null;
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    this.pending.clear();
    try { child.kill('SIGKILL'); } catch {}
  }

  request(payload, timeoutMs = DEFAULT_TIMEOUT) {
    if (this.pending.size >= this.maxPending) {
      return Promise.reject(Object.assign(new Error('Automation worker is busy.'), { code: 'automation_overloaded' }));
    }
    const started = performance.now();
    const workerStarted = this.start();
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (!this.pending.has(id)) return;
        // A timed-out synchronous worker has unknown execution state. Kill it
        // so later work cannot queue behind a command that may never return.
        const child = this.child;
        this.#failed(child, Object.assign(new Error('Automation timed out.'), { code: 'automation_timeout' }));
        try { child?.kill('SIGKILL'); } catch {}
      }, timeoutMs);
      this.pending.set(id, {
        resolve: message => resolve({
          ...message,
          timing: {
            ...message.timing,
            node_total_ms: Number((performance.now() - started).toFixed(3)),
            worker_started: workerStarted,
          },
        }),
        reject,
        timer,
      });
      this.child.stdin.write(`${JSON.stringify({ id, ...payload })}\n`, error => {
        if (!error) return;
        this.#failed(this.child, error);
      });
    });
  }

  close() {
    const child = this.child;
    if (child) this.#failed(child, Object.assign(new Error('Automation worker closed.'), { code: 'automation_closed' }));
    try { child?.kill('SIGTERM'); } catch {}
  }
}

export class AutomationPool {
  constructor({ size = Number(process.env.MOLA_AUTOMATION_WORKERS || 2), workerFactory } = {}) {
    if (!Number.isInteger(size) || size < 1 || size > 16) throw new Error('Automation worker count must be between 1 and 16.');
    this.workers = Array.from({ length: size }, () => workerFactory ? workerFactory() : new AutomationWorker());
    this.controlDir = statePath('automation', 'ssh');
    mkdirSync(this.controlDir, { recursive: true, mode: 0o700 });
  }

  async run(target, action, timeoutMs = DEFAULT_TIMEOUT) {
    const sessionKey = String(target.boot_id || target.runtime_generation || target.generation || 'local');
    const payload = {
      target: {
        id: target.id,
        session_key: sessionKey,
        ssh_host: target.ssh_host,
        ssh_port: target.ssh_port,
        display_host: target.display_host,
        display_port: target.display_port,
        ssh_key: join(statePath('keys'), 'guest'),
        known_hosts: join(statePath('keys'), 'known_hosts'),
        control_dir: this.controlDir,
      },
      action,
    };
    const digest = createHash('sha256').update(target.id).digest();
    const worker = this.workers[digest.readUInt32BE(0) % this.workers.length];
    const message = await worker.request(payload, timeoutMs);
    return process.env.MOLA_ACTION_TIMING === '1'
      ? { ...message.result, _mola_timing: message.timing }
      : message.result;
  }

  close() { for (const worker of this.workers) worker.close(); }
}

let pool;
export function runAction(target, action, timeoutMs = DEFAULT_TIMEOUT) {
  pool ??= new AutomationPool();
  return pool.run(target, action, timeoutMs);
}

export function closeAutomation() {
  pool?.close();
  pool = null;
}
