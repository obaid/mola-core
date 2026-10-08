import { randomUUID } from 'node:crypto';
import { revokeDesktop } from './desktop.js';
import { revokeSsh } from './ssh.js';
import { revokeDataPlane } from './data-plane.js';
import { revokeCua } from './cua.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const SNAPSHOT_CHUNK_BYTES = 8 * 1024 * 1024;
export const SNAPSHOT_CHUNK_ENCODED_BYTES = Math.ceil(SNAPSHOT_CHUNK_BYTES / 3) * 4;
// PHP's JSON encoder escapes `/` in base64 as `\/`. In the worst case the
// wire representation is twice the validated base64 length; validation below
// still caps the decoded payload at exactly SNAPSHOT_CHUNK_BYTES.
export const SNAPSHOT_CHUNK_BODY_BYTES = (2 * SNAPSHOT_CHUNK_ENCODED_BYTES) + (64 * 1024);
const fail = (status, message, code) => { throw Object.assign(new Error(message), { status, ...(code ? { code } : {}) }); };
const stable = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
export const STORAGE_VERBS = ['snapshot', 'checkpoint', 'restore', 'snapshot-delete', 'snapshot-import', 'snapshot-write', 'snapshot-read', 'snapshot-seal', 'snapshot-export', 'snapshot-import-direct', 'fence', 'resize'];
const fields = {
  snapshot: ['snapshot_id'], checkpoint: ['snapshot_id'], restore: ['snapshot_id', 'fork'], 'snapshot-delete': ['snapshot_id'],
  'snapshot-import': ['snapshot_id', 'manifest'], 'snapshot-write': ['snapshot_id', 'offset', 'data', 'sha256'],
  'snapshot-read': ['snapshot_id', 'offset'], 'snapshot-seal': ['snapshot_id'],
  'snapshot-export': ['snapshot_id', 'grant'], 'snapshot-import-direct': ['snapshot_id', 'manifest', 'grant'], fence: [],
  resize: ['vcpus', 'memory_mb', 'disk_gb'],
};

/** Durable intent for disk mutations; chunks use their offset and digest as an
 * idempotency identity and never put bulk payloads into the registry journal. */
export async function storageOperation(api, id, verb, body) {
  const record = api.record(id);
  const chunk = ['snapshot-write', 'snapshot-read', 'snapshot-export', 'snapshot-import-direct'].includes(verb);
  if (Object.keys(body).some(key => ![...fields[verb], ...(chunk ? [] : ['operation_id', 'generation'])].includes(key))) fail(400, 'Unknown storage operation field.');
  if (!['fence', 'resize'].includes(verb) && !UUID.test(body.snapshot_id || '')) fail(400, 'snapshot_id must be a UUID.');
  if (verb === 'resize') {
    if (!Number.isInteger(body.vcpus) || body.vcpus < 1 || body.vcpus > 8 || !Number.isInteger(body.memory_mb) || body.memory_mb < 1024 || body.memory_mb > 16384 || !Number.isInteger(body.disk_gb) || body.disk_gb < 16 || body.disk_gb > 1024) fail(400, 'Resize resources are invalid.');
  }
  if (verb === 'restore' && Object.hasOwn(body, 'fork') && typeof body.fork !== 'boolean') fail(400, 'fork must be a boolean.');
  if (chunk) {
    if (['snapshot-export', 'snapshot-import-direct'].includes(verb)) {
      if (!api.snapshotTransfer || !body.grant || typeof body.grant !== 'object' || Array.isArray(body.grant)) fail(400, 'Invalid direct snapshot transfer grant.');
      if (verb === 'snapshot-export') {
        const manifest = await api.runtime.snapshotManifest(id, body.snapshot_id);
        return { status: 200, body: { data: await api.snapshotTransfer.upload(id, body.snapshot_id, manifest, body.grant) } };
      }
      if (!body.manifest || typeof body.manifest !== 'object' || Array.isArray(body.manifest)) fail(400, 'Invalid snapshot manifest.');
      await api.runtime.storageOperation(id, 'snapshot-import', { snapshot_id: body.snapshot_id, manifest: body.manifest });
      const imported = await api.snapshotTransfer.download(id, body.snapshot_id, body.manifest, body.grant);
      if (imported.complete) await api.runtime.storageOperation(id, 'snapshot-seal', { snapshot_id: body.snapshot_id });
      return { status: 200, body: { data: imported } };
    }
    if (!Number.isSafeInteger(body.offset) || body.offset < 0) fail(400, 'offset must be a nonnegative integer.');
    if (verb === 'snapshot-write' && (typeof body.data !== 'string' || body.data.length > SNAPSHOT_CHUNK_ENCODED_BYTES
      || !/^[0-9a-f]{64}$/.test(body.sha256 || ''))) fail(400, 'Invalid snapshot chunk.');
    return { status: 200, body: { data: await api.runtime.storageOperation(id, verb, body) } };
  }
  if (!Number.isSafeInteger(body.generation) || body.generation < 1) fail(400, 'generation must be a positive integer.');
  if (typeof body.operation_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(body.operation_id)) fail(400, 'operation_id is required.');
  if (record.cloud.incarnation_generation && body.generation < record.cloud.incarnation_generation) fail(409, 'Operation belongs to a retired machine incarnation.');
  const fingerprint = stable(body), key = `${verb}:${body.operation_id}`;
  let operation = record.cloud.operations[key];
  if (operation) {
    if (operation.fingerprint !== fingerprint) fail(409, 'Operation ID was already used with a different payload.');
    if (operation.result) return operation.result;
    if (record.cloud.generation !== body.generation) fail(409, 'Pending operation was superseded.');
  } else {
    if (verb === 'checkpoint') {
      if ((await api.runtime.capabilities()).running_checkpoint !== true) fail(501, 'Running checkpoint qualification is disabled.', 'running_checkpoint_unsupported');
      const target = await api.runtime.describe(id);
      if (target.status !== 'running' || !record.boot_id) fail(409, 'Running checkpoint requires an enrolled running computer.');
      if (body.generation !== record.cloud.generation) fail(409, 'Checkpoint belongs to a different boot generation.');
    }
    if (verb === 'resize' && body.disk_gb < record.disk_gb) fail(400, 'Disk shrinking is unsupported.');
    if (body.generation < record.cloud.generation) fail(409, 'Stale boot generation.');
    if (Object.values(record.cloud.operations).some(value => !value.result)) fail(409, 'A pending operation must be reconciled first.');
    if (['snapshot', 'restore', 'resize'].includes(verb) && (await api.runtime.describe(id)).status !== 'stopped') fail(409, 'Stop the computer first.');
    record.cloud.generation = body.generation;
    record.runtime_generation = body.generation;
    operation = record.cloud.operations[key] = { fingerprint, verb, pending_at: new Date().toISOString() };
    if (verb === 'fence') {
      record.cloud.fenced = true;
      record.desired_state = 'stopped';
    }
    if (verb === 'restore') {
      // The restored guest may contain an earlier agent bearer token. Reset its
      // registration identity before the next boot; the seed wins over its cache.
      record.registration_token = randomUUID().replaceAll('-', '');
      record.machine_token_hash = null;
      record.boot_id = null;
      record.capabilities = null;
      record.last_heartbeat_at = null;
      record.desired_state = 'stopped';
    }
    api.registry.flush();
  }
  if (['restore', 'fence', 'resize'].includes(verb)) { revokeDesktop(id); revokeSsh(id); revokeDataPlane(id); revokeCua(id); }
  let data;
  if (operation.runtime_result) data = operation.runtime_result;
  else {
    const response = await api.runtime.storageOperation(id, verb, body);
    const receipt = response.storage_operation;
    if (receipt) {
      if (receipt.operation_id !== body.operation_id || receipt.verb !== verb || receipt.generation !== body.generation)
        fail(502, 'Native storage receipt identity mismatch.');
      if (receipt.status === 'pending') return pendingStorageResult(verb, body);
      if (receipt.status === 'failed') {
        // Restore admission rotated enrollment credentials. Even when the
        // original disk survived a rejected restore, reconcile its identity
        // before releasing the intent for a later start or deletion.
        if (verb === 'restore') await reseedRestore(api, id, record);
        const result = { status: receipt.error_status || 503, body: { error: receipt.error || 'Native storage operation failed.',
          code: 'storage_operation_failed', operation: { operation_id: body.operation_id, verb, generation: body.generation, status: 'failed' } } };
        operation.result = result;
        api.registry.flush();
        return result;
      }
      if (receipt.status !== 'completed' || !receipt.result) fail(502, 'Invalid native storage receipt.');
      data = receipt.result;
    } else data = response; // Compatibility with an older synchronous runtime.
    // Restore is already committed. A lost reseed reply must never decompress
    // and replace the guest disk a second time.
    operation.runtime_result = data;
    api.registry.flush();
  }
  if (verb === 'restore') await reseedRestore(api, id, record);
  if (verb === 'checkpoint' && (data.checkpoint_operation_id !== body.operation_id || !UUID.test(data.checkpoint_attempt || '')
    || data.consistency !== 'filesystem' || data.running_resumed !== true || data.filesystem_thawed !== true)) fail(502, 'Checkpoint release proof is incomplete.');
  if (verb === 'resize') {
    if (data.id !== id || data.status !== 'stopped' || !['vcpus', 'memory_mb', 'disk_gb'].every(k => data[k] === body[k])) fail(502, 'Invalid native resize result.');
    for (const field of ['vcpus', 'memory_mb', 'disk_gb']) { record[field] = data[field]; record.cloud.create_spec[field] = data[field]; }
  }
  const result = { status: ['snapshot', 'checkpoint'].includes(verb) ? 201 : 200, body: { data } };
  operation.result = result;
  api.registry.flush();
  return result;
}

export function pendingStorageResult(verb, body) {
  return { status: 202, body: { data: { operation_id: body.operation_id, verb,
    generation: body.generation, status: 'pending', retry_after_ms: 1000 } } };
}

/** Receipt polling reconciles the same persisted payload, including reseeding
 * after a core restart. It cannot manufacture a new operation identity. */
export async function storageReceipt(api, id, verb, operationId) {
  if (!STORAGE_VERBS.includes(verb) || !/^[a-zA-Z0-9_-]{1,128}$/.test(operationId || '')) fail(400, 'Invalid storage operation identity.');
  const record = api.record(id);
  const operation = record.cloud.operations[`${verb}:${operationId}`];
  if (!operation) fail(404, 'No such storage operation.');
  const body = JSON.parse(operation.fingerprint);
  return storageOperation(api, id, verb, body);
}

function reseedRestore(api, id, record) {
  return api.runtime.reseed(id, {
    registration_token: record.registration_token, authorized_keys: record.authorized_keys, name: record.name,
    browser_proxy: record.cloud.create_spec?.browser_proxy ?? null,
    network_binding: record.cloud.network_binding ?? null,
  });
}
