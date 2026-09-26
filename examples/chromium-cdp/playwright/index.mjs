import net from 'node:net';
import { chromium } from 'playwright';
import WebSocket from 'ws';
import { MolaClient } from '../../lib/mola-client.mjs';

const client = new MolaClient();
const machine = process.env.MOLA_MACHINE;
if (!machine) throw new Error('Set MOLA_MACHINE to a ready machine id.');
const profile = `/tmp/mola-cdp-${Date.now()}`;
await client.action(machine, {
  action: 'exec',
  command: `nohup chromium --headless=new --no-sandbox --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir=${profile} about:blank >/tmp/mola-cdp.log 2>&1 &`,
  timeout: 20,
});
for (let attempt = 0; attempt < 30; attempt += 1) {
  const probe = await client.action(machine, { action: 'exec', command: 'curl -fsS http://127.0.0.1:9222/json/version >/dev/null', timeout: 5 });
  if (probe.exit_code === 0) break;
  if (attempt === 29) throw new Error('Chromium CDP did not become ready. Inspect /tmp/mola-cdp.log.');
  await new Promise(resolve => setTimeout(resolve, 500));
}

async function grant() {
  const result = await client.request(`/machines/${encodeURIComponent(machine)}/tunnels`, {
    method: 'POST',
    body: { port: 9222 },
  });
  return client.dataPlaneUrl(result.tunnel_url);
}

const sockets = new Set();
const bridge = net.createServer(async local => {
  const remote = new WebSocket(await grant());
  sockets.add(remote);
  remote.binaryType = 'nodebuffer';
  const queued = [];
  local.on('data', chunk => remote.readyState === WebSocket.OPEN ? remote.send(chunk) : queued.push(chunk));
  remote.on('open', () => queued.splice(0).forEach(chunk => remote.send(chunk)));
  remote.on('message', (data, binary) => { if (binary) local.write(data); });
  remote.on('close', () => { sockets.delete(remote); local.destroy(); });
  remote.on('error', error => local.destroy(error));
  local.on('close', () => remote.close());
  local.on('error', () => remote.close());
});
await new Promise((resolve, reject) => {
  bridge.once('error', reject);
  bridge.listen(0, '127.0.0.1', resolve);
});

try {
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${bridge.address().port}`);
  const context = browser.contexts()[0] || await browser.newContext();
  const page = context.pages()[0] || await context.newPage();
  await page.setContent('<button id="counter">Clicked 0 times</button><script>let n=0;counter.onclick=()=>counter.textContent=`Clicked ${++n} times`</script>');
  await page.getByRole('button').click();
  const text = await page.getByRole('button').textContent();
  if (text !== 'Clicked 1 times') throw new Error(`Unexpected page state: ${text}`);
  console.log({ connected: true, title: await page.title(), assertion: text });
  await browser.close();
} finally {
  for (const socket of sockets) socket.close();
  await new Promise(resolve => bridge.close(resolve));
  await client.action(machine, { action: 'exec', command: `pkill -f -- '--user-data-dir=${profile}' || true; rm -rf ${profile}`, timeout: 20 });
}
