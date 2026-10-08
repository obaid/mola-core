import { spawn } from 'node:child_process';
import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { createServer } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { statePath, runtimeScript } from './paths.js';
import { installedImages } from './installed-images.js';

/** Ask the kernel for a free loopback port rather than hoping one is free. */
function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/**
 * Supervises the Python QEMU runner and speaks to it over loopback.
 *
 * The runner is kept as a separate process on purpose: it is the piece that has
 * actually booted a real Omarchy desktop, and rewriting proven QEMU supervision
 * in a second language to save one process would be trading working code for
 * novelty.
 */
export class Runtime {
  constructor(host, { port = null } = {}) {
    this.host = host;
    // Chosen at start time. A fixed port collides with anything else on this
    // machine that speaks the same protocol, which is exactly what happened
    // the first time this ran alongside another Mola deployment.
    this.port = port;
    this.base = null;
    this.child = null;
    this.token = null;
  }

  #writeConfig() {
    const root = statePath('runtime');
    mkdirSync(root, { recursive: true, mode: 0o700 });
    const config = {
      schema: 1,
      architecture: this.host.platform === 'darwin' ? 'aarch64' : 'x86_64',
      qemu: this.host.qemu,
      image: statePath('image'),
      default_image_ref: process.env.MOLA_IMAGE_REF || 'omarchy-agent:0.1.0',
      images: installedImages(),
      kernel_args: 'root=/dev/vda rw rootwait console=hvc0 systemd.unit=multi-user.target omarchy.qemu_virgl=1',
      // virtio-gpu-gl needs a GL context, and on macOS that context comes from
      // the cocoa display. This is the configuration measured to boot a real
      // Omarchy desktop; a headless QEMU display fails to bring the compositor
      // up. The desktop still leaves the machine over the guest's own VNC
      // server, so callers never touch this window.
      display: process.env.MOLA_DISPLAY
        || (this.host.acceleratedGraphics && this.host.platform === 'darwin'
          ? 'cocoa,gl=es,show-cursor=on,full-screen=off,full-grab=off'
          : 'none'),
      // Fall back to the plain device when QEMU has no virglrenderer: the
      // guest still gets a DRM node and renders with llvmpipe.
      gpu: process.env.MOLA_GPU || (this.host.acceleratedGraphics ? 'virtio-gpu-gl-pci' : 'virtio-gpu-pci'),
      connect_host: '127.0.0.1',
      guest_endpoint: `http://10.0.2.2:${process.env.MOLA_PORT || 4141}`,
      // Stock QEMU on HVF needs GICv3; the packaged runtime uses GICv2.
      gic_version: Number(process.env.MOLA_GIC || (this.host.acceleratedGraphics ? 2 : 3)),
      // Hosted restores require the operator-pinned daemon sidecar so older
      // snapshots can re-enrol after their credentials are rotated.
      guest_agent_refresh: (process.env.MOLA_GUEST_AGENT_REFRESH ?? process.env.MOLA_HOST_API) === '1',
      max_running: Number(process.env.MOLA_MAX_RUNNING || 2),
      max_memory_mb: Number(process.env.MOLA_MAX_MEMORY_MB || 8192),
    };
    const path = join(root, 'config.json');
    writeFileSync(path, JSON.stringify(config, null, 2), { mode: 0o600 });
    return path;
  }

  async start() {
    this.port ??= await freePort();
    this.base = `http://127.0.0.1:${this.port}`;
    const config = this.#writeConfig();
    this.child = spawn('python3', [runtimeScript('native/host.py'), '--config', config, '--port', String(this.port)], {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env },
    });
    this.child.stderr.on('data', (chunk) => process.stderr.write(`[runtime] ${chunk}`));

    // The runtime mints its own bearer token on first start and refuses every
    // request without it, health included.
    const tokenFile = join(statePath('runtime'), 'host.token');
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (existsSync(tokenFile)) { this.token = readFileSync(tokenFile, 'utf8').trim(); break; }
      if (this.child.exitCode !== null) throw new Error(`Runtime exited with code ${this.child.exitCode}`);
      await delay(50);
    }
    if (!this.token) throw new Error('Runtime never wrote its token.');

    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (await this.healthy()) return;
      if (this.child.exitCode !== null) throw new Error(`Runtime exited with code ${this.child.exitCode}`);
      await delay(100);
    }
    throw new Error('Runtime did not become healthy.');
  }

  async healthy() {
    try {
      const response = await fetch(`${this.base}/health`, {
        headers: { authorization: `Bearer ${this.token}` },
        signal: AbortSignal.timeout(2000),
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  async #call(method, path, body, timeout = 120_000) {
    const response = await fetch(this.base + path, {
      method,
      headers: {
        authorization: `Bearer ${this.token}`,
        ...(body ? { 'content-type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeout),
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : {};
    if (!response.ok) {
      throw Object.assign(new Error(payload.error || payload.message || 'Runtime call failed'), { status: response.status });
    }
    return payload;
  }

  async storageOperation(id, verb, body) {
    // Durable disk commands acknowledge intent promptly. Keep one request's
    // wait bounded; retrying the same identity reads the native receipt.
    let result = await this.#call('POST', `/machines/${id}/${verb}`, body, body.operation_id ? 10_000 : 120_000);
    if (!result.storage_operation) return result;
    const deadline = Date.now() + 1500;
    while (result.storage_operation.status === 'pending' && Date.now() < deadline) {
      await delay(100);
      result = await this.storageReceipt(id, verb, body.operation_id);
    }
    return result;
  }
  storageReceipt(id, verb, operationId) {
    return this.#call('GET', `/machines/${id}/storage-operations/${verb}/${operationId}`, undefined, 10_000);
  }
  snapshotManifest(id, snapshotId) { return this.#call('GET', `/machines/${id}/snapshots/${snapshotId}`); }
  reseed(id, body) { return this.#call('POST', `/machines/${id}/reseed`, body); }

  create(spec) { return this.#call('POST', '/machines', spec); }
  describe(id) { return this.#call('GET', `/machines/${id}`); }
  list() { return this.#call('GET', '/machines'); }
  startMachine(id) { return this.#call('POST', `/machines/${id}/start`); }
  shutdown(id) { return this.#call('POST', `/machines/${id}/shutdown`); }
  forceStop(id) { return this.#call('POST', `/machines/${id}/force-stop`); }
  destroy(id, deleteDisk = true) { return this.#call('DELETE', `/machines/${id}`, { delete_disk: Boolean(deleteDisk) }); }

  /**
   * Stop a machine and wait for the runtime to agree it has stopped.
   *
   * The runtime refuses to destroy a machine it still considers alive, and a
   * caller who has just asked to stop one should not have to poll before
   * asking to delete it.
   */
  async settle(id, { attempts = 40, every = 500, strict = false } = {}) {
    try { await this.forceStop(id); } catch (error) {
      if (strict && error.status !== 404) throw error;
    }
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      let described;
      try { described = await this.describe(id); } catch (error) {
        if (strict && error.status !== 404) throw error;
        return;
      }
      if (described.status === 'stopped') return;
      await delay(every);
    }
    throw new Error('The machine did not stop.');
  }

  stop() {
    this.child?.kill('SIGTERM');
  }

  imageReady() {
    return existsSync(join(statePath('image'), 'root.ext4'));
  }
}
