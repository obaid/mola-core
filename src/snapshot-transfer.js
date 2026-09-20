import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { open } from 'node:fs/promises';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { statePath } from './paths.js';

const SHA256 = /^[0-9a-f]{64}$/;
const MAX_PARTS = 10_000;
const MAX_PART_BYTES = 512 * 1024 * 1024;
const ALLOWED_HEADERS = new Set([
  'content-type',
  'x-amz-server-side-encryption-customer-algorithm',
  'x-amz-server-side-encryption-customer-key',
  'x-amz-server-side-encryption-customer-key-md5',
]);

const fail = message => { throw new Error(message); };

function safeR2Url(value) {
  let url;
  try { url = new URL(value); } catch { fail('Invalid snapshot transfer URL.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.hash
    || !url.hostname.endsWith('.r2.cloudflarestorage.com')) fail('Snapshot transfer URL is not an R2 S3 endpoint.');
  return url.toString();
}

function safeHeaders(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('Invalid snapshot transfer headers.');
  const headers = {};
  for (const [name, item] of Object.entries(value)) {
    const lower = name.toLowerCase();
    if (!ALLOWED_HEADERS.has(lower) || typeof item !== 'string' || item.length > 1024 || /[\r\n]/.test(item)) {
      fail('Invalid snapshot transfer header.');
    }
    headers[lower] = item;
  }
  return headers;
}

async function digest(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}

function save(path, value) {
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  renameSync(temporary, path);
}

/**
 * Moves immutable snapshot artifacts directly between the compute host and R2.
 * The control plane supplies short-lived, operation-specific signed requests;
 * no long-lived object-store credential is ever installed on a compute host.
 */
export class SnapshotTransfer {
  constructor({ fetcher = globalThis.fetch, root = statePath('runtime', 'snapshots'), now = () => Date.now() } = {}) {
    this.fetcher = fetcher;
    this.root = root;
    this.now = now;
  }

  folder(machineId, snapshotId) {
    return join(this.root, machineId, snapshotId);
  }

  validateIdentity(grant, manifest) {
    if (!grant || grant.version !== 1 || typeof grant.transfer_id !== 'string' || grant.transfer_id.length > 1024
      || grant.artifact_bytes !== manifest.artifact_bytes || grant.artifact_sha256 !== manifest.artifact_sha256
      || !Number.isSafeInteger(grant.artifact_bytes) || grant.artifact_bytes < 1 || !SHA256.test(grant.artifact_sha256 || '')) {
      fail('Snapshot transfer grant does not match the artifact.');
    }
  }

  async upload(machineId, snapshotId, manifest, grant) {
    this.validateIdentity(grant, manifest);
    if (!Array.isArray(grant.parts) || grant.parts.length > MAX_PARTS) fail('Invalid snapshot upload parts.');
    const folder = this.folder(machineId, snapshotId);
    const artifact = join(folder, 'disk.gz');
    if (!existsSync(artifact) || statSync(artifact).size !== manifest.artifact_bytes) fail('Snapshot artifact is missing or has changed size.');
    const receiptPath = join(folder, 'direct-upload.json');
    let receipt = existsSync(receiptPath) ? JSON.parse(readFileSync(receiptPath, 'utf8')) : null;
    const identity = { transfer_id: grant.transfer_id, artifact_bytes: manifest.artifact_bytes, artifact_sha256: manifest.artifact_sha256 };
    if (receipt && (receipt.transfer_id !== identity.transfer_id || receipt.artifact_bytes !== identity.artifact_bytes
      || receipt.artifact_sha256 !== identity.artifact_sha256)) fail('Snapshot upload grant changed during transfer.');
    receipt ??= { ...identity, verified: false, parts: {} };
    if (!receipt.verified) {
      if (await digest(artifact) !== manifest.artifact_sha256) fail('Snapshot artifact checksum mismatch before upload.');
      receipt.verified = true;
      save(receiptPath, receipt);
    }

    const supplied = grant.parts.map(part => {
      if (!part || !Number.isSafeInteger(part.number) || part.number < 1 || part.number > MAX_PARTS
        || !Number.isSafeInteger(part.offset) || part.offset < 0 || !Number.isSafeInteger(part.size)
        || part.size < 1 || part.size > MAX_PART_BYTES || part.offset + part.size > manifest.artifact_bytes) {
        fail('Invalid snapshot upload part.');
      }
      return { ...part, url: safeR2Url(part.url), headers: safeHeaders(part.headers || {}) };
    });
    if (new Set(supplied.map(part => part.number)).size !== supplied.length) fail('Duplicate snapshot upload part.');
    const concurrency = Math.min(8, Math.max(1, Number.isSafeInteger(grant.concurrency) ? grant.concurrency : 4));
    const deadline = this.now() + Math.min(240, Math.max(5, Number.isSafeInteger(grant.max_seconds) ? grant.max_seconds : 60)) * 1000;
    let cursor = 0;
    const worker = async () => {
      while (cursor < supplied.length && this.now() < deadline) {
        const part = supplied[cursor++];
        if (receipt.parts[String(part.number)]) continue;
        const response = await this.fetcher(part.url, {
          method: 'PUT', headers: { ...part.headers, 'content-length': String(part.size) },
          body: createReadStream(artifact, { start: part.offset, end: part.offset + part.size - 1 }), duplex: 'half',
          redirect: 'error', signal: AbortSignal.timeout(120_000),
        });
        if (!response.ok) fail(`R2 rejected snapshot part ${part.number} with HTTP ${response.status}.`);
        const etag = response.headers.get('etag');
        if (!etag || etag.length > 256 || /[\r\n]/.test(etag)) fail('R2 returned an invalid multipart receipt.');
        receipt.parts[String(part.number)] = etag;
        save(receiptPath, receipt);
      }
    };
    const workers = await Promise.allSettled(Array.from({ length: Math.min(concurrency, supplied.length || 1) }, worker));
    const failed = workers.find(result => result.status === 'rejected');
    if (failed) throw failed.reason;
    const parts = Object.entries(receipt.parts).map(([number, etag]) => ({ part_number: Number(number), etag }))
      .sort((a, b) => a.part_number - b.part_number);
    const uploadedBytes = parts.reduce((total, part) => {
      const suppliedPart = supplied.find(item => item.number === part.part_number);
      return total + (suppliedPart?.size || 0);
    }, 0);
    return { complete: supplied.every(part => receipt.parts[String(part.number)]), uploaded_bytes: uploadedBytes, parts };
  }

  async download(machineId, snapshotId, manifest, grant) {
    this.validateIdentity(grant, manifest);
    const url = safeR2Url(grant.url);
    const headers = safeHeaders(grant.headers || {});
    const folder = this.folder(machineId, snapshotId);
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    const complete = join(folder, 'disk.gz');
    if (existsSync(complete)) {
      if (statSync(complete).size !== manifest.artifact_bytes || await digest(complete) !== manifest.artifact_sha256) {
        fail('Existing snapshot artifact does not match the archive.');
      }
      return { complete: true, downloaded_bytes: manifest.artifact_bytes };
    }
    const target = join(folder, 'upload.partial');
    const offset = existsSync(target) ? statSync(target).size : 0;
    if (offset > manifest.artifact_bytes) fail('Partial snapshot exceeds the archive size.');
    const controller = new AbortController();
    const seconds = Math.min(240, Math.max(5, Number.isSafeInteger(grant.max_seconds) ? grant.max_seconds : 60));
    const timer = setTimeout(() => controller.abort(), seconds * 1000);
    try {
      const response = await this.fetcher(url, {
        headers: { ...headers, ...(offset ? { range: `bytes=${offset}-` } : {}) },
        redirect: 'error', signal: controller.signal,
      });
      if (!response.ok || (offset && response.status !== 206) || !response.body) fail(`R2 rejected snapshot download with HTTP ${response.status}.`);
      await pipeline(Readable.fromWeb(response.body), createWriteStream(target, { flags: offset ? 'a' : 'w', mode: 0o600 }));
    } catch (error) {
      if (error?.name !== 'AbortError') throw error;
    } finally {
      clearTimeout(timer);
    }
    const downloaded = existsSync(target) ? statSync(target).size : 0;
    if (downloaded > manifest.artifact_bytes) fail('Downloaded snapshot exceeds its declared size.');
    if (downloaded < manifest.artifact_bytes) return { complete: false, downloaded_bytes: downloaded };
    if (await digest(target) !== manifest.artifact_sha256) fail('Downloaded snapshot checksum mismatch.');
    const handle = await open(target, 'r');
    try { await handle.sync(); } finally { await handle.close(); }
    renameSync(target, complete);
    return { complete: true, downloaded_bytes: downloaded };
  }
}
