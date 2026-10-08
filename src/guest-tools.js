import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runtimeScript, statePath } from './paths.js';
import { serializedComputerTool } from './tool-locks.js';

const SOURCE = readFileSync(runtimeScript('guest_tools.py'), 'utf8');
const PROXY_SOURCE = readFileSync(runtimeScript('browser_proxy.py'), 'utf8');
const fail = (status, code) => Object.assign(new Error(code.replaceAll('_', ' ')), { status, code });
export const COMPUTER_TOOLS = {
  browser: new Set(['browser_prepare', 'browser_navigate', 'browser_snapshot', 'browser_click', 'browser_fill', 'browser_key', 'browser_evaluate', 'browser_screenshot', 'browser_tabs', 'browser_capabilities']),
  apps: new Set(['app_install', 'app_status', 'app_launch', 'app_list']),
  'session-manifest': new Set(['session_save', 'session_restore', 'session_state', 'session_autosave']),
  'computer-tools': new Set(['loopback_http', 'viewer_input', 'window_identity', 'software_state', 'software_pin', 'software_stage', 'software_activate', 'software_rollback']),
  'network-tools': new Set(['network_configure', 'network_status', 'network_teardown']),
};

/** Credentials and requests travel exclusively on private SSH stdin. They are
 * never interpolated into a command, persisted in host receipts, or logged. */
export class GuestTools {
  constructor({ spawnImpl = spawn, source = null } = {}) { this.spawn = spawnImpl; this.source = source; }

  run(id, target, kind, body, binding) {
    return serializedComputerTool(id, () => this.execute(id, target, kind, body, binding));
  }

  async execute(id, target, kind, body, binding) {
    if (COMPUTER_TOOLS[kind] && !COMPUTER_TOOLS[kind].has(body?.tool)) throw fail(400, 'unsupported_computer_tool');
    if (!COMPUTER_TOOLS[kind] && !['geometry', 'capture', 'view-input', 'vault-inject'].includes(kind)) throw fail(400, 'unsupported_computer_tool');
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw fail(400, 'invalid_tool_arguments');
    if (['kind', 'binding', 'source', 'proxy_source'].some(k => Object.hasOwn(body, k))) throw fail(400, 'invalid_tool_arguments');
    const host = target.ssh_host === 'host.docker.internal' ? '127.0.0.1' : target.ssh_host;
    if (!['127.0.0.1', '::1', 'localhost'].includes(host) || !Number.isInteger(target.ssh_port)
      || target.ssh_port < 1 || target.ssh_port > 65535) throw fail(409, 'private_guest_connection_required');
    const request = JSON.stringify({ source: this.source ?? readFileSync(runtimeScript('guest_tools.py'), 'utf8'), proxy_source: PROXY_SOURCE,
      session_source: readFileSync(runtimeScript('session_agent.py'),'utf8'), request: { ...body, kind, binding } });
    if (Buffer.byteLength(request) > 2 * 1024 * 1024) throw fail(413, 'tool_request_too_large');
    // The fixed bootstrap contains no caller data. Never enable inherited SSH
    // debug output or relay stderr from browser/application subprocesses.
    const bootstrap = "python3 -c 'import json,sys; p=json.load(sys.stdin); n={\"__source__\":p[\"source\"],\"__proxy_source__\":p[\"proxy_source\"],\"__session_source__\":p[\"session_source\"]}; exec(p[\"source\"],n); print(json.dumps(n[\"dispatch\"](p[\"request\"])))'";
    const args = ['-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=10',
      '-o', 'StrictHostKeyChecking=accept-new', '-o', `HostKeyAlias=mola-${id}`,
      '-o', `UserKnownHostsFile=${join(statePath('keys'), 'known_hosts')}`,
      '-i', join(statePath('keys'), 'guest'), '-p', String(target.ssh_port), `dev@${host}`, bootstrap];
    return new Promise((resolve, reject) => {
      const child = this.spawn('ssh', args, { stdio: ['pipe', 'pipe', 'ignore'] });
      let output = '', settled = false;
      const finish = (error, result) => { if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve(result); };
      const timer = setTimeout(() => { child.kill('SIGKILL'); finish(fail(504, 'tool_outcome_unknown')); }, 45_000);
      child.stdout.on('data', data => {
        output += data;
        if (Buffer.byteLength(output) > 24 * 1024 * 1024) { child.kill('SIGKILL'); finish(fail(502, 'tool_response_too_large')); }
      });
      child.on('error', () => finish(fail(502, 'guest_tool_transport_failed')));
      child.on('close', code => {
        if (code !== 0) return finish(fail(502, 'tool_outcome_unknown'));
        try {
          const reply = JSON.parse(output);
          if (reply.ok === true && reply.result && typeof reply.result === 'object') {
            if (kind === 'vault-inject') return reply.result.success === true
              ? finish(null, { success: true }) : finish(fail(503, 'secret_injection_failed'));
            return finish(null, reply.result);
          }
          const safeCode = /^[a-z_]{1,80}$/.test(reply.code || '') ? reply.code : 'guest_tool_failed';
          finish(fail([400, 409, 422, 501, 503].includes(reply.status) ? reply.status : 503, safeCode));
        } catch { finish(fail(502, 'invalid_guest_tool_response')); }
      });
      child.stdin.on('error', () => finish(fail(502, 'tool_outcome_unknown')));
      child.stdin.end(request);
    });
  }
}
