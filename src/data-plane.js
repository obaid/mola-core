import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { WebSocketServer } from 'ws';
import { validateAction } from './api.js';
import { statePath } from './paths.js';

const tickets = new Map();
const active = new Set();
const hash = value => createHash('sha256').update(value).digest('hex');
const MAX_IN_FLIGHT = 16;
const MAX_BINARY = 1024 * 1024;
const MAX_SESSION_MS = 30 * 60_000;
const IDLE_MS = 60_000;
const MAX_TUNNEL_BYTES = 32 * 1024 * 1024;
const MAX_TUNNELS_PER_MACHINE = 4;
const LATENCY_BUCKETS = [10, 25, 50, 100, 250, 500, 1000, 5000];
const metrics = { sessions: 0, actions: 0, bytes_in: 0, bytes_out: 0, stale_rejects: 0,
  backpressure_rejects: 0, tunnels: 0, action_latency_ms: {} };

export function getDataPlaneMetrics() { return JSON.parse(JSON.stringify({ ...metrics, active: active.size })); }

function observe(action, elapsed) {
  const histogram = metrics.action_latency_ms[action] ??= {
    count: 0, sum: 0, min: null, max: null, buckets: Object.fromEntries(LATENCY_BUCKETS.map(limit => [limit, 0])), overflow: 0,
  };
  histogram.count += 1; histogram.sum += elapsed;
  histogram.min = histogram.min === null ? elapsed : Math.min(histogram.min, elapsed);
  histogram.max = histogram.max === null ? elapsed : Math.max(histogram.max, elapsed);
  const bucket = LATENCY_BUCKETS.find(limit => elapsed <= limit);
  if (bucket) histogram.buckets[bucket] += 1; else histogram.overflow += 1;
}

function mint(kind, target) {
  const now = Date.now();
  for (const [key, entry] of tickets) if (entry.ticket_expires <= now || entry.revoked) tickets.delete(key);
  const value = randomBytes(32).toString('base64url');
  tickets.set(hash(value), { kind, ...target, ticket_expires: now + 60_000,
    session_expires: now + MAX_SESSION_MS, revoked: false });
  return value;
}

export function mintActionTicket(id, target, binding) {
  return mint('actions', { id, target, binding });
}

export function mintTunnelTicket(id, target, port, binding) {
  if (!Number.isInteger(port) || port < 1024 || port > 65535 || [4141, 5900, 22].includes(port)) {
    throw Object.assign(new Error('Tunnel port must be an allowed guest port between 1024 and 65535.'), { status: 400 });
  }
  return mint('tunnel', { id, target, port, binding });
}

export function revokeDataPlane(id) {
  for (const [key, entry] of tickets) if (entry.id === id) { entry.revoked = true; tickets.delete(key); }
  for (const entry of active) if (entry.id === id) {
    entry.revoked = true;
    entry.socket?.close(4001, 'machine lifecycle changed');
    entry.tunnel?.kill('SIGTERM');
  }
}

function redeem(value, kind) {
  const key = hash(value || '');
  const entry = tickets.get(key);
  tickets.delete(key);
  return entry?.kind === kind && entry.ticket_expires > Date.now() ? entry : null;
}

async function allowed(entry) {
  try { return !entry.revoked && entry.session_expires > Date.now() && await entry.binding.validate(entry.binding); }
  catch { return false; }
}

function sendJson(socket, body) {
  if (socket.readyState === 1) {
    const payload = JSON.stringify(body);
    metrics.bytes_out += Buffer.byteLength(payload);
    socket.send(payload);
  }
}

function binaryResult(socket, id, result) {
  const encoded = result.image_base64 || result.content_base64;
  if (!encoded) return false;
  const data = Buffer.from(encoded, 'base64');
  sendJson(socket, { id, ok: true, binary: {
    bytes: data.length,
    mime_type: result.mime_type || 'application/octet-stream',
    sha256: createHash('sha256').update(data).digest('hex'),
    ...(result.size !== undefined ? { size: result.size } : {}),
  } });
  metrics.bytes_out += data.length;
  socket.send(data, { binary: true });
  return true;
}

function actionSocket(socket, entry, runAction) {
  entry.socket = socket;
  active.add(entry);
  let queued = 0;
  let chain = Promise.resolve();
  let awaitingBinary = null;
  let binaryTimer = null;
  let subscription = null;
  let frameBusy = false;
  let frame;
  let previous;
  const requestIds = new Set();
  let idleTimer;
  const lifetimeTimer = setTimeout(() => socket.close(4001, 'session expired'), Math.max(1, entry.session_expires - Date.now()));
  const touch = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => socket.close(4000, 'session idle timeout'), IDLE_MS);
  };
  const stopSubscription = () => { if (subscription) clearInterval(subscription); subscription = null; };
  const execute = message => {
    queued += 1;
    chain = chain.then(async () => {
      if (!await allowed(entry)) {
        metrics.stale_rejects += 1;
        throw Object.assign(new Error('Action session is stale.'), { code: 'stale_session' });
      }
      let action;
      try { action = validateAction(message.action); }
      catch (error) { throw Object.assign(error, { code: 'invalid_action' }); }
      const started = performance.now();
      const result = await runAction(entry.target, action);
      observe(action.action, Number((performance.now() - started).toFixed(3)));
      metrics.actions += 1;
      if (!(message.binary && binaryResult(socket, message.id, result))) sendJson(socket, { id: message.id, ok: true, result });
    }).catch(error => sendJson(socket, { id: message.id, ok: false, error: { code: error.code || 'action_failed', message: error.message } }))
      .finally(() => { queued -= 1; });
  };
  metrics.sessions += 1;
  touch();
  sendJson(socket, { type: 'ready', protocol: 1, max_in_flight: MAX_IN_FLIGHT, max_binary_bytes: MAX_BINARY });
  socket.on('message', (raw, isBinary) => {
    touch();
    metrics.bytes_in += raw.length;
    if (isBinary) {
      if (!awaitingBinary) return sendJson(socket, { ok: false, error: { code: 'unexpected_binary', message: 'No binary write is pending.' } });
      const data = Buffer.from(raw);
      const pending = awaitingBinary;
      awaitingBinary = null;
      clearTimeout(binaryTimer); binaryTimer = null;
      if (data.length !== pending.binary_bytes || data.length > MAX_BINARY) {
        return sendJson(socket, { id: pending.id, ok: false, error: { code: 'invalid_binary', message: 'Binary length did not match the request.' } });
      }
      pending.action.content_base64 = data.toString('base64');
      execute(pending);
      return;
    }
    let message;
    try { message = JSON.parse(raw.toString()); } catch { return sendJson(socket, { ok: false, error: { code: 'invalid_json', message: 'Message must be JSON.' } }); }
    if (message.op === 'ping') return sendJson(socket, { id: message.id, op: 'pong' });
    if (typeof message.id !== 'string' || !message.id || requestIds.has(message.id)) {
      return sendJson(socket, { id: message.id, ok: false, error: { code: 'invalid_request_id', message: 'Request IDs must be unique non-empty strings.' } });
    }
    requestIds.add(message.id);
    if (message.op === 'screen.unsubscribe') { stopSubscription(); return sendJson(socket, { id: message.id, ok: true }); }
    if (message.op === 'screen.resync') {
      previous = undefined;
      sendJson(socket, { id: message.id, ok: true });
      frame?.();
      return;
    }
    if (message.op === 'screen.subscribe') {
      stopSubscription();
      const fps = Math.max(1, Math.min(10, Number(message.max_fps || 2)));
      frame = async () => {
        if (frameBusy || socket.bufferedAmount > 2 * MAX_BINARY || !await allowed(entry)) return;
        frameBusy = true;
        try {
          const result = await runAction(entry.target, { action: 'screenshot' });
          const data = Buffer.from(result.image_base64, 'base64');
          const digest = createHash('sha256').update(data).digest('hex');
          if (digest !== previous) {
            previous = digest;
            sendJson(socket, { type: 'screen.frame', subscription_id: message.id, bytes: data.length, mime_type: result.mime_type, digest });
            metrics.bytes_out += data.length;
            socket.send(data, { binary: true });
          }
        } catch (error) { sendJson(socket, { type: 'screen.error', subscription_id: message.id, message: error.message }); }
        finally { frameBusy = false; }
      };
      subscription = setInterval(frame, Math.ceil(1000 / fps));
      sendJson(socket, { id: message.id, ok: true, max_fps: fps });
      frame();
      return;
    }
    if (message.op !== 'action' || queued >= MAX_IN_FLIGHT) {
      if (queued >= MAX_IN_FLIGHT) metrics.backpressure_rejects += 1;
      return sendJson(socket, { id: message.id, ok: false, error: { code: queued >= MAX_IN_FLIGHT ? 'backpressure' : 'invalid_request', message: 'Invalid or overloaded action request.' } });
    }
    if (message.binary_bytes !== undefined) {
      if (awaitingBinary || message.action?.action !== 'write_file' || !Number.isInteger(message.binary_bytes) || message.binary_bytes < 0 || message.binary_bytes > MAX_BINARY) {
        return sendJson(socket, { id: message.id, ok: false, error: { code: 'invalid_binary', message: 'Invalid or interleaved binary write request.' } });
      }
      awaitingBinary = message;
      binaryTimer = setTimeout(() => {
        if (awaitingBinary !== message) return;
        awaitingBinary = null;
        sendJson(socket, { id: message.id, ok: false, error: { code: 'binary_timeout', message: 'Binary payload was not received in time.' } });
      }, 5000);
      return;
    }
    execute(message);
  });
  socket.on('close', () => { stopSubscription(); clearTimeout(idleTimer); clearTimeout(binaryTimer); clearTimeout(lifetimeTimer); active.delete(entry); });
}

function tunnelSocket(socket, entry, spawnImpl) {
  entry.socket = socket;
  if ([...active].filter(item => item.id === entry.id && item.tunnel).length >= MAX_TUNNELS_PER_MACHINE) {
    return socket.close(4008, 'tunnel connection limit');
  }
  active.add(entry);
  if (!['127.0.0.1', '::1', 'localhost', 'host.docker.internal'].includes(entry.target.ssh_host)) return socket.close(4003, 'invalid machine target');
  const digest = hash(`${entry.id}:${entry.target.boot_id || entry.target.session_key || ''}`).slice(0, 24);
  const args = ['-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=10',
    '-o', 'StrictHostKeyChecking=accept-new', '-o', `HostKeyAlias=mola-${entry.id}-${digest.slice(0, 8)}`,
    '-o', `UserKnownHostsFile=${join(statePath('keys'), 'known_hosts')}`, '-i', join(statePath('keys'), 'guest'),
    '-p', String(entry.target.ssh_port), '-W', `127.0.0.1:${entry.port}`, `dev@${entry.target.ssh_host}`];
  const child = spawnImpl('ssh', args, { stdio: ['pipe', 'pipe', 'ignore'] });
  entry.tunnel = child;
  metrics.tunnels += 1;
  let bytes = 0;
  let idleTimer;
  const lifetimeTimer = setTimeout(() => socket.close(4001, 'tunnel expired'), Math.max(1, entry.session_expires - Date.now()));
  const touch = () => { clearTimeout(idleTimer); idleTimer = setTimeout(() => socket.close(4000, 'tunnel idle timeout'), IDLE_MS); };
  const account = length => { bytes += length; return bytes <= MAX_TUNNEL_BYTES; };
  touch();
  child.stdout.on('data', data => {
    touch();
    if (!account(data.length)) return socket.close(4009, 'tunnel byte limit');
    if (socket.readyState === 1) socket.send(data, { binary: true });
  });
  socket.on('message', (data, isBinary) => {
    touch();
    if (!isBinary) return socket.close(1003, 'tunnel accepts binary frames only');
    if (!account(data.length)) return socket.close(4009, 'tunnel byte limit');
    child.stdin.write(data);
  });
  socket.on('close', () => { clearTimeout(idleTimer); clearTimeout(lifetimeTimer); active.delete(entry); child.kill('SIGTERM'); });
  child.on('close', () => socket.close());
  child.on('error', () => socket.close(1011, 'tunnel failed'));
}

export function attachDataPlane(server, { runAction, spawnImpl = spawn }) {
  const sockets = new WebSocketServer({ noServer: true, maxPayload: MAX_BINARY + 4096 });
  server.on('upgrade', async (request, socket, head) => {
    const url = new URL(request.url, 'http://localhost');
    const kind = url.pathname === '/actions/socket' ? 'actions' : url.pathname === '/tunnel/socket' ? 'tunnel' : null;
    // Other upgrade handlers own the desktop and SSH transports. Leaving an
    // unknown path alone is essential because Node invokes every upgrade
    // listener for the same request.
    if (!kind) return;
    const entry = redeem(url.searchParams.get('t'), kind);
    if (!entry || request.headers.origin || !(await allowed(entry))) {
      socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n'); return socket.destroy();
    }
    sockets.handleUpgrade(request, socket, head, client => {
      if (kind === 'actions') actionSocket(client, entry, runAction);
      else tunnelSocket(client, entry, spawnImpl);
    });
  });
}
