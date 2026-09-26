import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { statePath } from './paths.js';

// Management, configuration, local file export, and policy tools stay inside
// the guest. The public API exposes the interactive computer-use surface.
const TOOLS = new Set([
  'list_apps', 'list_windows', 'get_accessibility_tree', 'get_window_state',
  'get_desktop_state', 'get_screen_size', 'get_cursor_position',
  'get_browser_state', 'browser_prepare', 'browser_navigate', 'browser_click',
  'browser_pointer', 'browser_type', 'browser_dialog', 'page', 'click',
  'double_click', 'right_click', 'move_cursor', 'drag', 'scroll', 'press_key',
  'hotkey', 'type_text', 'set_value', 'invoke_menu', 'set_window_frame',
  'verify_state', 'zoom', 'launch_app', 'bring_to_front', 'clipboard_read',
  'clipboard_write', 'check_permissions', 'health_report',
]);
const fail = (status, message) => { throw Object.assign(new Error(message), { status }); };
const sessions = new Map();
const IDLE_MS = 30 * 60_000;

function evict(id, entry) {
  clearTimeout(entry.timer);
  entry.mcp.close();
  sessions.delete(id);
}

function renew(id, entry) {
  clearTimeout(entry.timer);
  entry.expiresAt = Date.now() + IDLE_MS;
  entry.timer = setTimeout(() => evict(id, entry), IDLE_MS);
  entry.timer.unref();
}

export class CuaMcpProcess {
  constructor(child) {
    this.child = child;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = '';
    this.closed = false;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => this.receive(chunk));
    child.on('error', () => this.close());
    child.on('close', () => this.close());
  }

  receive(chunk) {
    this.buffer += chunk;
    if (this.buffer.length > 24 * 1024 * 1024) return this.close();
    for (let index; (index = this.buffer.indexOf('\n')) !== -1;) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      let packet;
      try { packet = JSON.parse(line); } catch { continue; }
      const waiting = this.pending.get(packet.id);
      if (!waiting) continue;
      this.pending.delete(packet.id);
      clearTimeout(waiting.timer);
      if (packet.error) waiting.reject(new Error(packet.error.message || 'Cua rejected the request.'));
      else waiting.resolve(packet.result);
    }
  }

  send(method, params = {}, timeoutMs = 90_000) {
    if (this.closed) return Promise.reject(new Error('Cua session disconnected.'));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.close();
        reject(new Error('Cua call timed out; its outcome is unknown. Inspect before retrying.'));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`, error => {
        if (error) this.close();
      });
    });
  }

  notify(method) {
    if (!this.closed) this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method })}\n`);
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    for (const waiting of this.pending.values()) {
      clearTimeout(waiting.timer);
      waiting.reject(new Error('Cua session disconnected; the last action outcome may be unknown.'));
    }
    this.pending.clear();
    this.child.stdin.destroy();
    this.child.kill('SIGTERM');
  }
}

function guestProcess(id, target) {
  const host = target.ssh_host === 'host.docker.internal' ? '127.0.0.1' : target.ssh_host;
  if (!['127.0.0.1', '::1', 'localhost'].includes(host)
    || !Number.isInteger(target.ssh_port) || target.ssh_port < 1 || target.ssh_port > 65535) {
    fail(409, 'Cua requires a private host-loopback guest connection.');
  }
  const args = ['-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
    '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new',
    '-o', `HostKeyAlias=mola-${id}`,
    '-o', `UserKnownHostsFile=${join(statePath('keys'), 'known_hosts')}`,
    '-i', join(statePath('keys'), 'guest'), '-p', String(target.ssh_port),
    `dev@${host}`,
    'exec /usr/local/bin/cua-driver mcp --socket "$HOME/.cache/cua-driver/cua-driver.sock"'];
  return new CuaMcpProcess(spawn('ssh', args, { stdio: ['pipe', 'pipe', 'ignore'] }));
}

function purge() {
  for (const [id, entry] of sessions) {
    if (entry.expiresAt <= Date.now() || entry.mcp.closed) {
      evict(id, entry);
    }
  }
}

export function revokeCua(machineId) {
  for (const [id, entry] of sessions) {
    if (entry.machineId === machineId) evict(id, entry);
  }
}

export class CuaSessions {
  constructor(launch = guestProcess) { this.launch = launch; }

  async create(machineId, target, binding, actor) {
    purge();
    if (typeof actor !== 'string' || !/^[A-Za-z0-9:_-]{1,128}$/.test(actor)) fail(400, 'actor is required.');
    if ([...sessions.values()].filter(entry => entry.machineId === machineId).length >= 8) fail(429, 'Too many Cua sessions on this computer.');
    const mcp = this.launch(machineId, target);
    try {
      const init = await mcp.send('initialize', {
        protocolVersion: '2025-06-18', capabilities: {},
        clientInfo: { name: 'mola-core', version: '1.3.0' },
      }, 15_000);
      if (init?.serverInfo?.name !== 'cua-driver') throw new Error('Unexpected Cua MCP server.');
      mcp.notify('notifications/initialized');
      const listed = await mcp.send('tools/list', {}, 15_000);
      const tools = (listed?.tools || []).filter(tool => TOOLS.has(tool.name));
      if (!tools.length) throw new Error('Cua MCP exposed no computer-use tools.');
      const id = randomUUID();
      const entry = { machineId, binding, actor, mcp, tools: new Map(tools.map(tool => [tool.name, tool])) };
      sessions.set(id, entry);
      renew(id, entry);
      return { id, version: init.serverInfo.version, expires_in: IDLE_MS / 1000, tools };
    } catch (error) { mcp.close(); throw error; }
  }

  get(machineId, sessionId, actor, binding) {
    purge();
    const entry = sessions.get(sessionId);
    if (!entry || entry.machineId !== machineId || entry.actor !== actor) fail(404, 'Cua session not found.');
    if (entry.binding.generation !== binding.generation || entry.binding.boot_id !== binding.boot_id) {
      evict(sessionId, entry); fail(409, 'Cua session belongs to a previous boot.');
    }
    return entry;
  }

  async call(machineId, sessionId, actor, binding, tool, args) {
    const entry = this.get(machineId, sessionId, actor, binding);
    if (!entry.tools.has(tool)) fail(400, 'Cua tool is unavailable or not exposed by Mola.');
    if (args === null || typeof args !== 'object' || Array.isArray(args)) fail(400, 'arguments must be an object.');
    renew(sessionId, entry);
    try { return await entry.mcp.send('tools/call', { name: tool, arguments: args }); }
    catch (error) { if (entry.mcp.closed) evict(sessionId, entry); throw error; }
  }

  end(machineId, sessionId, actor, binding) {
    const entry = this.get(machineId, sessionId, actor, binding);
    evict(sessionId, entry);
    return { ended: true };
  }
}
