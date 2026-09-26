import { existsSync, readFileSync } from 'node:fs';
import { statePath } from '../paths.js';

/**
 * A client of the engine's public REST API.
 *
 * Deliberately a client and not a second engine. Two processes writing
 * `machines.json` would corrupt it, so this one owns no state, supervises no
 * QEMU and holds no lock. Everything it can do, any other caller can do, which
 * makes it an honest test of whether the public API is sufficient.
 */

/** Read the operator token without creating one. Its absence is information. */
function readToken() {
  const file = statePath('token');
  return existsSync(file) ? readFileSync(file, 'utf8').trim() : null;
}

export class EngineDown extends Error {
  constructor(detail) {
    super(detail);
    this.name = 'EngineDown';
  }
}

export class EngineError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function createEngine({
  base = process.env.MOLA_API || `http://127.0.0.1:${process.env.MOLA_PORT || 4141}`,
  token = process.env.MOLA_TOKEN || readToken(),
  fetchImpl = fetch,
} = {}) {
  async function call(method, path, body, { timeoutMs = 180_000 } = {}) {
    if (!token) {
      throw new EngineDown('No engine token. Start the engine first with: npx mola-core');
    }

    // A machine action can legitimately take minutes. An abort signal keeps a
    // hung guest from wedging the session with no way back.
    const abort = AbortSignal.timeout(timeoutMs);
    let response;
    try {
      response = await fetchImpl(`${base}${path}`, {
        method,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body ? { 'content-type': 'application/json' } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
        signal: abort,
      });
    } catch (error) {
      if (error.name === 'TimeoutError' || error.name === 'AbortError') {
        throw new EngineError(504, `The engine did not answer within ${Math.round(timeoutMs / 1000)}s.`);
      }
      // ECONNREFUSED and friends all mean the same thing to a user.
      throw new EngineDown(`No engine at ${base}. Start it with: npx mola-core`);
    }

    const text = await response.text();
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      throw new EngineError(response.status, `The engine replied with something that is not JSON: ${text.slice(0, 200)}`);
    }

    if (!response.ok) {
      throw new EngineError(response.status, payload.message ?? `HTTP ${response.status}`);
    }
    return payload.data ?? payload;
  }

  return {
    base,
    hasToken: () => Boolean(token),
    info: () => call('GET', '/v1'),
    health: () => call('GET', '/health', undefined, { timeoutMs: 5_000 }),

    listMachines: () => call('GET', '/v1/machines'),
    getMachine: (id) => call('GET', `/v1/machines/${encodeURIComponent(id)}`),
    createMachine: (spec) => call('POST', '/v1/machines', spec),
    startMachine: (id) => call('POST', `/v1/machines/${encodeURIComponent(id)}/start`),
    stopMachine: (id, force) => call('POST', `/v1/machines/${encodeURIComponent(id)}/stop`, force ? { force: true } : undefined),
    deleteMachine: (id) => call('DELETE', `/v1/machines/${encodeURIComponent(id)}`),
    desktop: (id) => call('POST', `/v1/machines/${encodeURIComponent(id)}/desktop`),

    act: (id, action, { timeoutMs } = {}) =>
      call('POST', `/v1/machines/${encodeURIComponent(id)}/actions`, action, { timeoutMs }),
  };
}

/**
 * Poll until a machine is usable.
 *
 * `ready` means the guest daemon has checked in for this boot, which is the only
 * signal that the machine will actually answer an action. A running QEMU process
 * is not the same thing, and treating it as one produces failures that look
 * random.
 */
export async function waitForReady(engine, id, { timeoutMs = 120_000, intervalMs = 1000, sleep } = {}) {
  const pause = sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  const deadline = Date.now() + timeoutMs;
  let last = null;

  while (Date.now() < deadline) {
    const machine = await engine.getMachine(id);
    last = machine.status;
    if (last === 'ready') return machine;
    if (last === 'stopped' || last === 'failed') {
      throw new EngineError(409, `The machine reached "${last}" instead of becoming ready.`);
    }
    await pause(intervalMs);
  }
  throw new EngineError(504, `The machine was still "${last}" after ${Math.round(timeoutMs / 1000)}s.`);
}
