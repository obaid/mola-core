import net from 'node:net';
import { chromium } from 'playwright';
import WebSocket from 'ws';

const api = (process.env.MOLA_API || 'http://127.0.0.1:4141/v1').replace(/\/$/, '');
const token = process.env.MOLA_TOKEN;
const machine = process.env.MOLA_MACHINE;
if (!token || !machine) throw new Error('Set MOLA_TOKEN and MOLA_MACHINE.');

async function grant() {
  const response = await fetch(`${api}/machines/${encodeURIComponent(machine)}/tunnels`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ port: 9222 }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `Tunnel grant failed (${response.status}).`);
  return payload.data.tunnel_url;
}

const bridge = net.createServer(async local => {
  try {
    const remote = new WebSocket(await grant());
    remote.binaryType = 'nodebuffer';
    const queued = [];
    local.on('data', chunk => remote.readyState === WebSocket.OPEN ? remote.send(chunk) : queued.push(chunk));
    remote.on('open', () => queued.splice(0).forEach(chunk => remote.send(chunk)));
    remote.on('message', (data, binary) => { if (binary) local.write(data); });
    remote.on('close', () => local.destroy());
    remote.on('error', error => local.destroy(error));
    local.on('close', () => remote.close());
    local.on('error', () => remote.close());
  } catch (error) {
    local.destroy(error);
  }
});
await new Promise((resolve, reject) => {
  bridge.once('error', reject);
  bridge.listen(0, '127.0.0.1', resolve);
});
const port = bridge.address().port;
try {
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  console.log(`Connected to Chromium with ${browser.contexts().length} context(s).`);
  await browser.close();
} finally {
  await new Promise(resolve => bridge.close(resolve));
}
