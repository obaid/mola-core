import { installedImages } from './installed-images.js';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { statePath } from './paths.js';

const ACTIONS = new Set(['exec', 'read_file', 'write_file', 'screenshot', 'click', 'move', 'scroll', 'type', 'key']);

export function operatorToken() {
  const file = statePath('token');
  if (existsSync(file)) return readFileSync(file, 'utf8').trim();
  const token = randomBytes(24).toString('base64url');
  writeFileSync(file, `${token}\n`, { mode: 0o600 });
  return token;
}

export function authorised(request, token) {
  const header = request.headers.authorization ?? '';
  const presented = header.startsWith('Bearer ') ? header.slice(7) : '';
  const a = createHash('sha256').update(presented).digest();
  const b = createHash('sha256').update(token).digest();
  return timingSafeEqual(a, b);
}

/**
 * Map the runtime's view onto a status a caller can act on.
 *
 * `unknown` is a real answer and is reported as such. A runtime that cannot see
 * a machine has not told us the machine stopped, and collapsing the two is how
 * a platform ends up billing for a computer that died, or losing one it still
 * holds.
 */
export function presentStatus(runtimeStatus, record) {
  if (runtimeStatus === 'stopped') return 'stopped';
  if (runtimeStatus === 'starting') return 'starting';
  if (runtimeStatus === 'unknown') return 'unknown';
  // Running is necessary but not sufficient: the desktop is usable only once
  // the guest has reported in for *this* boot.
  const beat = record?.last_heartbeat_at ? Date.parse(record.last_heartbeat_at) : 0;
  const fresh = Date.now() - beat < 60_000;
  return fresh && record?.capabilities?.shell ? 'ready' : 'booting';
}

export function present(record, described) {
  return {
    id: record.id,
    name: record.name,
    status: presentStatus(described?.status, record),
    runtime_status: described?.status ?? 'unknown',
    vcpus: record.vcpus,
    memory_mb: record.memory_mb,
    disk_gb: record.disk_gb,
    image_ref: record.cloud?.image_ref ?? record.image_ref ?? process.env.MOLA_IMAGE_REF ?? 'omarchy-agent:0.1.0',
    created_at: record.created_at,
    capabilities: record.capabilities,
    last_heartbeat_at: record.last_heartbeat_at,
    ssh: described ? { host: described.ssh_host, port: described.ssh_port, user: 'dev' } : null,
  };
}

export function defaultResources() {
  const configuredDefault = (key, fallback) => {
    if (process.env[key] === undefined || process.env[key] === '') return fallback;
    const value = Number(process.env[key]);
    if (!Number.isInteger(value)) throw new Error(`${key} must be an integer.`);
    return value;
  };
  return {
    vcpus: configuredDefault('MOLA_DEFAULT_VCPUS', 4),
    memory_mb: configuredDefault('MOLA_DEFAULT_MEMORY_MB', 4096),
    disk_gb: configuredDefault('MOLA_DEFAULT_DISK_GB', 40),
  };
}

export function validateSpec(body) {
  const name = typeof body?.name === 'string' && body.name.trim() ? body.name.trim().slice(0, 64) : `omarchy-${Date.now()}`;
  const defaults = defaultResources();
  const vcpus = Number.isInteger(body?.vcpus) ? body.vcpus : defaults.vcpus;
  const memory_mb = Number.isInteger(body?.memory_mb) ? body.memory_mb : defaults.memory_mb;
  const disk_gb = Number.isInteger(body?.disk_gb) ? body.disk_gb : defaults.disk_gb;
  if (vcpus < 1 || vcpus > 8) throw new Error('vcpus must be between 1 and 8.');
  if (memory_mb < 1024 || memory_mb > 16384) throw new Error('memory_mb must be between 1024 and 16384.');
  if (disk_gb < 16 || disk_gb > 1024) throw new Error('disk_gb must be between 16 and 1024.');
  return { name, vcpus, memory_mb, disk_gb, ...(Object.hasOwn(body || {}, 'display') ? { display: validateDisplay(body.display) } : {}) };
}

export function validateDisplay(display) {
  if (!display || typeof display !== 'object' || Array.isArray(display) || Object.keys(display).some(k => !['width', 'height'].includes(k))
    || !Number.isInteger(display.width) || !Number.isInteger(display.height) || display.width < 640 || display.width > 3840 || display.height < 480 || display.height > 2160
    || display.width * display.height > 8294400) throw new Error('display requires width 640..3840 and height 480..2160.');
  return { width: display.width, height: display.height };
}

export function validateLocalSpec(body, images = installedImages(), defaultRef = process.env.MOLA_IMAGE_REF || 'omarchy-agent:0.1.0') {
  if (!Object.hasOwn(body, 'image_ref')) return validateSpec(body);
  const ref = body.image_ref;
  if (typeof ref !== 'string' || (ref !== defaultRef && !Object.hasOwn(images, ref))) {
    throw new Error('Requested image is not installed on this host.');
  }
  const defaults = images[ref]?.default_resources || {};
  return { ...validateSpec({ ...defaults, ...body }), image_ref: ref };
}

export function validateAction(body) {
  const action = body?.action;
  if (!ACTIONS.has(action)) throw new Error(`action must be one of: ${[...ACTIONS].join(', ')}`);
  if (action === 'exec' && typeof body.command !== 'string') throw new Error('exec requires a command string.');
  if (Object.hasOwn(body, 'job_run_id') && (typeof body.job_run_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(body.job_run_id))) throw new Error('job_run_id must be a bounded operation identity.');
  if (Object.hasOwn(body, 'payload') && Buffer.byteLength(JSON.stringify(body.payload)) > 1024 * 1024) throw new Error('Job payload exceeds 1 MiB.');
  if ((action === 'read_file' || action === 'write_file') && typeof body.path !== 'string') throw new Error('path is required.');
  if (action === 'write_file') {
    const text = typeof body.content === 'string';
    const binary = typeof body.content_base64 === 'string';
    if (text === binary) throw new Error('write_file requires exactly one of content or content_base64.');
    if (binary) {
      const decoded = Buffer.from(body.content_base64, 'base64');
      if (decoded.length > 1024 * 1024 || decoded.toString('base64').replace(/=+$/, '') !== body.content_base64.replace(/=+$/, '')) {
        throw new Error('content_base64 must be valid base64 no larger than 1 MiB.');
      }
    }
  }
  if (action === 'type' && typeof body.text !== 'string') throw new Error('text is required.');
  if (action === 'key' && typeof body.key !== 'string') throw new Error('key is required.');
  return body;
}
