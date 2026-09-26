import { createHash } from 'node:crypto';
import WebSocket from 'ws';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export class MolaApiError extends Error {
  constructor(message, { status, code, body } = {}) {
    super(message);
    this.name = 'MolaApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

export class MolaClient {
  constructor({ api = process.env.MOLA_API || 'http://127.0.0.1:4141/v1', token = process.env.MOLA_TOKEN, fetchImpl = fetch } = {}) {
    if (!token) throw new Error('Set MOLA_TOKEN to the Mola operator token.');
    this.api = api.replace(/\/$/, '');
    this.token = token;
    this.fetch = fetchImpl;
  }

  async request(path, { method = 'GET', body, timeoutMs = 180_000 } = {}) {
    let response;
    try {
      response = await this.fetch(this.api + path, {
        method,
        headers: { authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : { 'content-type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      if (error.name === 'TimeoutError' || error.name === 'AbortError') {
        throw new MolaApiError(`Mola did not answer ${method} ${path} within ${Math.round(timeoutMs / 1000)} seconds.`, { code: 'timeout' });
      }
      throw new MolaApiError(`Could not reach Mola at ${this.api}: ${error.message}`, { code: 'unreachable' });
    }
    const text = await response.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; }
    catch { throw new MolaApiError(`Mola returned non-JSON for ${method} ${path}.`, { status: response.status, body: text.slice(0, 300) }); }
    if (!response.ok) throw new MolaApiError(payload.message || `Mola returned HTTP ${response.status}.`, { status: response.status, body: payload });
    return payload.data ?? payload;
  }

  createMachine(spec) { return this.request('/machines', { method: 'POST', body: spec }); }
  getMachine(id) { return this.request(`/machines/${encodeURIComponent(id)}`); }
  listMachines() { return this.request('/machines'); }
  startMachine(id) { return this.request(`/machines/${encodeURIComponent(id)}/start`, { method: 'POST', body: {} }); }
  stopMachine(id, { force = false } = {}) { return this.request(`/machines/${encodeURIComponent(id)}/stop`, { method: 'POST', body: force ? { force: true } : {} }); }
  deleteMachine(id) { return this.request(`/machines/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
  action(id, action, options = {}) { return this.request(`/machines/${encodeURIComponent(id)}/actions`, { method: 'POST', body: action, ...options }); }
  metrics() { return this.request('/metrics', { timeoutMs: 10_000 }); }

  dataPlaneUrl(grantedUrl) {
    const granted = new URL(grantedUrl);
    const api = new URL(this.api);
    granted.protocol = api.protocol === 'https:' ? 'wss:' : 'ws:';
    granted.host = api.host;
    return granted.toString();
  }

  async waitForStatus(id, wanted, { timeoutMs = 180_000, intervalMs = 1000 } = {}) {
    const accepted = new Set(Array.isArray(wanted) ? wanted : [wanted]);
    const deadline = Date.now() + timeoutMs;
    let last = 'unknown';
    while (Date.now() < deadline) {
      const machine = await this.getMachine(id);
      last = machine.status;
      if (accepted.has(last)) return machine;
      if (['failed', 'error'].includes(last)) throw new MolaApiError(`Machine ${id} reached ${last} while waiting for ${[...accepted].join(' or ')}.`, { code: 'machine_failed' });
      await sleep(intervalMs);
    }
    throw new MolaApiError(`Machine ${id} was still ${last} after ${Math.round(timeoutMs / 1000)} seconds.`, { code: 'machine_timeout' });
  }

  async actionSession(id, options = {}) {
    const grant = await this.request(`/machines/${encodeURIComponent(id)}/session`, { method: 'POST', body: {} });
    return ActionSession.connect(this.dataPlaneUrl(grant.session_url), options);
  }
}

export class ActionSession {
  static async connect(url, { WebSocketImpl = WebSocket, timeoutMs = 15_000 } = {}) {
    const socket = new WebSocketImpl(url, { origin: undefined });
    const session = new ActionSession(socket, timeoutMs);
    await session.ready;
    return session;
  }

  constructor(socket, timeoutMs = 30_000) {
    this.socket = socket;
    this.timeoutMs = timeoutMs;
    this.nextId = 1;
    this.pending = new Map();
    this.binary = null;
    this.closed = null;
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Action session did not become ready.')), timeoutMs);
      socket.on('message', (raw, isBinary) => this.#message(raw, isBinary, resolve, timer));
      socket.once('error', error => { clearTimeout(timer); reject(error); this.#failAll(error); });
      socket.once('close', (code, reason) => {
        clearTimeout(timer);
        const error = new MolaApiError(`Action session closed (${code}: ${reason || 'no reason'}). Obtain a new session; do not replay an uncertain action.`, { code: code === 4001 ? 'stale_session' : 'session_closed' });
        this.closed = error;
        this.#failAll(error);
      });
    });
  }

  #message(raw, isBinary, readyResolve, readyTimer) {
    if (isBinary) {
      const pending = this.binary;
      this.binary = null;
      if (!pending) return this.#failAll(new Error('Received an unexpected binary frame.'));
      const data = Buffer.from(raw);
      if (data.length !== pending.meta.bytes || createHash('sha256').update(data).digest('hex') !== pending.meta.sha256) {
        return pending.reject(new Error('Binary response failed its length or SHA-256 check.'));
      }
      clearTimeout(pending.timer);
      this.pending.delete(pending.id);
      pending.resolve({ ...pending.message, data });
      return;
    }
    const message = JSON.parse(raw.toString());
    if (message.type === 'ready') {
      clearTimeout(readyTimer);
      this.capabilities = message;
      readyResolve(message);
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    if (message.ok && message.binary) {
      this.binary = { ...pending, message, meta: message.binary };
      return;
    }
    clearTimeout(pending.timer);
    this.pending.delete(message.id);
    if (message.ok === false) pending.reject(new MolaApiError(message.error?.message || 'Action failed.', { code: message.error?.code, body: message }));
    else pending.resolve(message.result ?? message);
  }

  #failAll(error) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  async action(action, { binary = false, data, timeoutMs = this.timeoutMs } = {}) {
    if (this.closed) throw this.closed;
    const id = String(this.nextId++);
    const request = { id, op: 'action', action, ...(binary ? { binary: true } : {}), ...(data ? { binary_bytes: data.length } : {}) };
    const promise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new MolaApiError(`Action ${id} timed out. Its outcome is unknown, so it was not replayed.`, { code: 'action_timeout' }));
      }, timeoutMs);
      this.pending.set(id, { id, resolve, reject, timer });
    });
    this.socket.send(JSON.stringify(request));
    if (data) this.socket.send(data, { binary: true });
    return promise;
  }

  close() { this.socket.close(1000, 'client finished'); }
}
