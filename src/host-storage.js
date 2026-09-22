import { randomUUID } from 'node:crypto';
import { revokeDesktop } from './desktop.js';
import { revokeSsh } from './ssh.js';
import { revokeDataPlane } from './data-plane.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const SNAPSHOT_CHUNK_BYTES = 8 * 1024 * 1024;
export const SNAPSHOT_CHUNK_ENCODED_BYTES = Math.ceil(SNAPSHOT_CHUNK_BYTES / 3) * 4;
// PHP's JSON encoder escapes `/` in base64 as `\/`. In the worst case the
// wire representation is twice the validated base64 length; validation below
// still caps the decoded payload at exactly SNAPSHOT_CHUNK_BYTES.
export const SNAPSHOT_CHUNK_BODY_BYTES = (2 * SNAPSHOT_CHUNK_ENCODED_BYTES) + (64 * 1024);
const fail = (status, message) => { throw Object.assign(new Error(message), { status }); };
const stable = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
export const STORAGE_VERBS = ['snapshot', 'restore', 'snapshot-delete', 'snapshot-import', 'snapshot-write', 'snapshot-read', 'snapshot-seal', 'snapshot-export', 'snapshot-import-direct', 'fence'];
const fields = {
  snapshot: ['snapshot_id'], restore: ['snapshot_id', 'fork'], 'snapshot-delete': ['snapshot_id'],
  'snapshot-import': ['snapshot_id', 'manifest'], 'snapshot-write': ['snapshot_id', 'offset', 'data', 'sha256'],
  'snapshot-read': ['snapshot_id', 'offset'], 'snapshot-seal': ['snapshot_id'],
  'snapshot-export': ['snapshot_id', 'grant'], 'snapshot-import-direct': ['snapshot_id', 'manifest', 'grant'], fence: [],
};

/** Durable intent for disk mutations; chunks use their offset and digest as an
 * idempotency identity and never put bulk payloads into the registry journal. */
export async function storageOperation(api, id, verb, body) {
  const record = api.record(id);
  const chunk = ['snapshot-write', 'snapshot-read', 'snapshot-export', 'snapshot-import-direct'].includes(verb);
  if (Object.keys(body).some(key => ![...fields[verb], ...(chunk ? [] : ['operation_id', 'generation'])].includes(key))) fail(400, 'Unknown storage operation field.');
  if (verb !== 'fence' && !UUID.test(body.snapshot_id || '')) fail(400, 'snapshot_id must be a UUID.');
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
    if (body.generation < record.cloud.generation) fail(409, 'Stale boot generation.');
    if (Object.values(record.cloud.operations).some(value => !value.result)) fail(409, 'A pending operation must be reconciled first.');
    if (['snapshot', 'restore'].includes(verb) && (await api.runtime.describe(id)).status !== 'stopped') fail(409, 'Stop the computer first.');
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
  if (['restore', 'fence'].includes(verb)) { revokeDesktop(id); revokeSsh(id); revokeDataPlane(id); }
  const data = await api.runtime.storageOperation(id, verb, body);
  if (verb === 'restore') await api.runtime.reseed(id, {
    registration_token: record.registration_token, authorized_keys: record.authorized_keys, name: record.name,
  });
  const result = { status: verb === 'snapshot' ? 201 : 200, body: { data } };
  operation.result = result;
  api.registry.flush();
  return result;
}
