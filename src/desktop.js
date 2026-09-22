import { randomBytes, createHash } from 'node:crypto';
import { connect } from 'node:net';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { join, normalize, extname } from 'node:path';
import { createRequire } from 'node:module';
import { WebSocketServer } from 'ws';

const require = createRequire(import.meta.url);

/**
 * The installed noVNC, served from disk: no CDN, works offline.
 *
 * Resolved through the one path the package's export map exposes, because
 * asking for its package.json is blocked by that same map.
 */
function novncRoot() {
  return join(require.resolve('@novnc/novnc'), '..', '..');
}

const TYPES = { '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.json': 'application/json' };

/**
 * Serve one file from the noVNC package.
 *
 * The path is normalised and confined to the package directory: this is a
 * local service, but a path-traversal hole is a path-traversal hole.
 */
export function serveNovnc(pathname, response) {
  const root = novncRoot();
  const target = normalize(join(root, pathname.replace(/^\/novnc\//, '')));
  if (!target.startsWith(root) || !existsSync(target) || !statSync(target).isFile()) {
    response.writeHead(404, { 'content-type': 'text/plain' });
    return response.end('Not found');
  }
  response.writeHead(200, { 'content-type': TYPES[extname(target)] ?? 'application/octet-stream' });
  createReadStream(target).pipe(response);
}

/**
 * Single-use tickets for the desktop.
 *
 * The ticket travels in the URL *fragment*, which browsers never send to a
 * server. That keeps it out of access logs, proxy logs and Referer headers —
 * the page reads `location.hash` and opens the socket itself.
 */
const tickets = new Map();
const activeTargets = new Set();
const TTL_MS = 60_000;

export function mintTicket(machineId, described, binding = null) {
  const ticket = randomBytes(24).toString('base64url');
  tickets.set(createHash('sha256').update(ticket).digest('hex'), {
    machineId, binding, revoked: false,
    host: described.display_host === 'host.docker.internal' ? '127.0.0.1' : described.display_host,
    port: described.display_port,
    expires: Date.now() + TTL_MS,
  });
  return ticket;
}

function redeem(ticket) {
  const key = createHash('sha256').update(ticket ?? '').digest('hex');
  const entry = tickets.get(key);
  if (!entry) return null;
  tickets.delete(key); // single use
  return entry.expires > Date.now() ? entry : null;
}

/** Revoke both unredeemed tickets and pending/connected transports before mutation. */
export function revokeDesktop(machineId) {
  for (const [key, target] of tickets) {
    if (target.machineId === machineId) { target.revoked = true; tickets.delete(key); }
  }
  for (const target of activeTargets) {
    if (target.machineId === machineId) {
      target.revoked = true;
      target.upstream?.destroy();
      target.client?.terminate();
    }
  }
}

export function attachDesktop(server) {
  const sockets = new WebSocketServer({ noServer: true });

  server.on('upgrade', async (request, socket, head) => {
    const url = new URL(request.url, 'http://localhost');
    if (url.pathname !== '/desktop/socket') return;

    const target = redeem(url.searchParams.get('t'));
    if (!target) {
      socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
      return socket.destroy();
    }

    // Keep the lease visible while the asynchronous runtime check is pending.
    // Stop/start/destroy can revoke it during that await, before any dial.
    activeTargets.add(target);
    socket.once('close', () => activeTargets.delete(target));
    let allowed = !target.revoked;
    try { if (target.binding) allowed = allowed && await target.binding.validate(target.binding); }
    catch { allowed = false; }
    if (!allowed || target.revoked || target.expires <= Date.now() || socket.destroyed) {
      activeTargets.delete(target);
      if (!socket.destroyed) socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
      return socket.destroy();
    }

    sockets.handleUpgrade(request, socket, head, (client) => {
      target.client = client;
      const upstream = connect(target.port, target.host);
      target.upstream = upstream;
      upstream.on('error', () => client.close());
      client.on('error', () => upstream.destroy());
      upstream.on('data', (chunk) => client.readyState === 1 && client.send(chunk));
      client.on('message', (data) => upstream.write(data));
      client.on('close', () => upstream.destroy());
      upstream.on('close', () => client.close());
    });
  });
}

export function desktopPage() {
  return `<!doctype html>
<meta charset="utf-8">
<title>Omarchy</title>
<style>
  html,body{margin:0;height:100%;background:#16161e;color:#c0caf5;
    font:14px ui-monospace,SFMono-Regular,Menlo,monospace}
  #screen{width:100vw;height:100vh}
  #note{position:fixed;inset:0;display:grid;place-items:center;text-align:center;padding:2rem}
</style>
<div id="screen"></div>
<div id="note">connecting…</div>
<script type="module">
  const note = document.getElementById('note');
  const say = (text) => { note.textContent = text; if (!note.isConnected) document.body.append(note); };

  // A viewer that fails silently is worse than one that fails loudly: the
  // operator is left staring at "connecting" with nothing to act on.
  addEventListener('error', (event) => say('Failed to start: ' + (event.message || event.error)));
  addEventListener('unhandledrejection', (event) => say('Failed to start: ' + event.reason));

  try {
    const { default: RFB } = await import('/novnc/core/rfb.js');
    const ticket = location.hash.slice(1).replace(/^t=/, '');
    if (!ticket) throw new Error('No ticket in the URL. Ask the API for a desktop URL.');

    history.replaceState(null, '', location.pathname);   // keep it out of history
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const rfb = new RFB(
      document.getElementById('screen'),
      scheme + '://' + location.host + '/desktop/socket?t=' + encodeURIComponent(ticket),
    );
    rfb.scaleViewport = true;
    rfb.addEventListener('connect', () => note.remove());
    rfb.addEventListener('disconnect', (event) => say(
      'Disconnected' + (event.detail && event.detail.clean === false ? ' unexpectedly' : '') +
      '. Ask the API for a new desktop URL.',
    ));
    rfb.addEventListener('securityfailure', () => say('The desktop refused the connection.'));
    setTimeout(() => { if (note.isConnected && note.textContent === 'connecting\u2026') say('Still connecting. Is the machine ready?'); }, 10000);
  } catch (error) {
    say('Failed to start: ' + error.message);
  }
</script>`;
}
