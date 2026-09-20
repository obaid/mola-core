import { randomBytes, randomUUID } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { statePath } from './paths.js';
import { authorised, validateSpec, validateAction } from './api.js';
import { revokeDesktop } from './desktop.js';
import { handleSsh, revokeSsh } from './ssh.js';
import { storageOperation, STORAGE_VERBS } from './host-storage.js';

const fail = (status, message, code) => { throw Object.assign(new Error(message), { status, code }); };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const canonical = value => JSON.stringify(value, Object.keys(value).sort());

export function hostToken(operator, env = process.env) {
  if (env.MOLA_HOST_API !== '1') return null;
  const file = env.MOLA_HOST_TOKEN_FILE || statePath('host-api.token');
  let token = env.MOLA_HOST_TOKEN;
  if (!token && existsSync(file)) token = readFileSync(file, 'utf8').trim();
  if (!token) {
    token = randomBytes(32).toString('base64url');
    writeFileSync(file, `${token}\n`, { mode: 0o600, flag: 'wx' });
  }
  if (token.length < 32 || token === operator) throw new Error('Host API requires a distinct token of at least 32 characters.');
  return token;
}

export function hostDescription(record, runtime, settledOperationKey = null) {
  let status = ['running', 'starting', 'stopped'].includes(runtime?.status) ? runtime.status : 'unknown';
  const pending = Object.entries(record.cloud.operations || {})
    .filter(([key, operation]) => key !== settledOperationKey && !operation.result);
  if (pending.length) {
    // A stopped observation is not release evidence while an earlier command
    // may still complete. Keep admission held until that intent is reconciled.
    const starting = pending.every(([key, operation]) => ['create', 'start'].includes(operation.verb || key.split(':')[0]));
    status = starting && runtime?.status !== 'unknown' && runtime ? 'starting' : 'unknown';
  }
  const heartbeat = Date.parse(record.last_heartbeat_at || '');
  const afterStart = heartbeat >= Date.parse(record.cloud?.boot_requested_at || '');
  const caps = record.capabilities;
  const ready = !record.cloud.fenced && status === 'running' && record.desired_state === 'running'
    && Boolean(record.boot_id) && !record.cloud?.previous_boot_ids?.includes(record.boot_id)
    && afterStart && heartbeat <= Date.now() && Date.now() - heartbeat < 60_000
    && Boolean(caps?.shell && caps?.display && caps?.sshd);
  return {
    id: record.id, name: record.name, status, disk_id: runtime?.disk_id ?? record.id,
    generation: record.cloud.generation, fenced: Boolean(record.cloud.fenced), image_ref: record.cloud.image_ref,
    vcpus: record.vcpus, memory_mb: record.memory_mb, disk_gb: record.disk_gb,
    ready, boot_id: record.boot_id ?? null, capabilities: caps ?? null,
    last_heartbeat_at: record.last_heartbeat_at ?? null,
  };
}

/** Host-only control contract. Intent and credentials are saved before runtime work.
 * A pending operation is retried against the same runtime ID, never a new guest.
 * Tombstones and results are retained to fence delayed messages after deletion.
 */
export class HostApi {
  constructor({ registry, runtime, publicKey, token, imageRef = process.env.MOLA_IMAGE_REF || 'omarchy-agent:0.1.0', desktop, action, snapshotTransfer = null }) {
    Object.assign(this, { registry, runtime, publicKey, token, imageRef, desktop, action, snapshotTransfer });
    this.locks = new Map();
  }

  async locked(id, work) {
    const previous = this.locks.get(id) || Promise.resolve();
    const next = previous.catch(() => {}).then(work);
    this.locks.set(id, next);
    try { return await next; } finally { if (this.locks.get(id) === next) this.locks.delete(id); }
  }

  record(id) {
    const record = this.registry.get(id);
    if (!record?.cloud || record.cloud.deleted) fail(404, 'No such managed machine.', 'machine_not_found');
    return record;
  }

  async describe(record, settledOperationKey = null) {
    const revision = () => JSON.stringify([record.cloud.generation, record.cloud.deleted,
      Object.entries(record.cloud.operations || {}).map(([key, operation]) => [key, Boolean(operation.result)])]);
    const before = revision();
    let runtime;
    try { runtime = await this.runtime.describe(record.id); } catch { /* uncertainty is not stopped */ }
    // A lifecycle call could complete while this read was in flight. Its old
    // stopped snapshot must not override a newer accepted start.
    if (before !== revision()) runtime = { status: 'unknown' };
    return hostDescription(record, runtime, settledOperationKey);
  }

  async handle(request, parts, body = {}) {
    if (!this.token) fail(404, 'Not found.');
    if (!authorised(request, this.token)) fail(401, 'Unauthenticated.');
    if (request.headers.origin) fail(403, 'Browser origins are not accepted.');
    const method = request.method;
    if (parts[0] !== 'machines') fail(404, 'Not found.');
    if (method === 'GET' && parts.length === 1) {
      const records = this.registry.all().filter(record => record.cloud && !record.cloud.deleted);
      return { status: 200, body: { data: await Promise.all(records.map(record => this.describe(record))) } };
    }
    if (method === 'GET' && parts.length === 2) {
      return { status: 200, body: { data: await this.describe(this.record(parts[1])) } };
    }
    if (method === 'GET' && parts.length === 4 && parts[2] === 'snapshots') {
      this.record(parts[1]);
      if (!UUID.test(parts[3] || '')) fail(400, 'snapshot_id must be a UUID.');
      return { status: 200, body: { data: await this.runtime.snapshotManifest(parts[1], parts[3]) } };
    }
    if (method !== 'POST') fail(405, 'Method not allowed.');
    const verb = parts.length === 1 ? 'create' : parts[2];
    const id = verb === 'create' ? body.id : parts[1];
    if (!UUID.test(id || '')) fail(400, 'id must be a UUID.');
    if (parts.length === 3 && verb === 'ssh') return this.locked(id, () => handleSsh(this, id, body));
    if (parts.length === 3 && STORAGE_VERBS.includes(verb)) return this.locked(id, () => storageOperation(this, id, verb, body));
    if (parts.length === 3 && ['desktop', 'actions'].includes(verb)) {
      return this.locked(id, async () => {
        const record = this.record(id);
        const described = await this.runtime.describe(id);
        if (described.status !== 'running') fail(409, 'Machine is not running.');
        if (verb === 'desktop') {
          if (!hostDescription(record, described).ready) fail(409, 'Machine is not ready.');
          const binding = { generation: record.cloud.generation, boot_id: record.boot_id };
          return { status: 201, body: { data: this.desktop(id, described, binding) } };
        }
        let action;
        try { action = validateAction(body); } catch (error) { fail(400, error.message); }
        return { status: 200, body: { data: await this.action({ id: record.id, ...described }, action) } };
      });
    }
    if (!(parts.length === 1 || parts.length === 3) || !['create', 'start', 'shutdown', 'force-stop', 'destroy'].includes(verb)) fail(404, 'Not found.');
    return this.locked(id, () => this.mutate(id, verb, body));
  }

  async validateDesktop(id, binding) {
    return this.locked(id, async () => {
      const record = this.registry.get(id);
      if (!record?.cloud || record.cloud.deleted || record.cloud.generation !== binding.generation
        || !binding.boot_id || record.boot_id !== binding.boot_id) return false;
      const described = await this.runtime.describe(id);
      return !record.cloud.deleted && record.cloud.generation === binding.generation
        && record.boot_id === binding.boot_id && hostDescription(record, described).ready;
    });
  }

  async mutate(id, verb, body) {
    if (!Number.isSafeInteger(body.generation) || body.generation < 1) fail(400, 'generation must be a positive integer.');
    if (typeof body.operation_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(body.operation_id)) fail(400, 'operation_id is required (letters, numbers, underscores or hyphens; maximum 128).');
    const allowed = verb === 'create'
      ? ['id', 'name', 'vcpus', 'memory_mb', 'disk_gb', 'image_ref', 'operation_id', 'generation', 'recreate']
      : ['operation_id', 'generation', ...(verb === 'destroy' ? ['delete_disk'] : [])];
    if (Object.keys(body).some(key => !allowed.includes(key))) fail(400, 'Unknown operation field.');
    if (verb === 'create' && Object.hasOwn(body, 'recreate') && typeof body.recreate !== 'boolean') fail(400, 'recreate must be a boolean.');
    if (verb === 'destroy' && typeof body.delete_disk !== 'boolean') fail(400, 'destroy requires explicit delete_disk.');
    let spec;
    if (verb === 'create') {
      if (!['vcpus', 'memory_mb', 'disk_gb'].every(key => Number.isInteger(body[key])) || typeof body.name !== 'string' || !body.name.trim()) fail(400, 'A name and integer resource sizes are required.');
      try { spec = validateSpec(body); } catch (error) { fail(400, error.message); }
    }
    const fingerprint = canonical(body);
    const key = `${verb}:${body.operation_id}`;
    let record = this.registry.get(id);
    // An ID reused after confirmed disk deletion is a new incarnation. Old
    // completed replies must not describe this new disk as deleted or running.
    if (record?.cloud?.incarnation_generation && body.generation < record.cloud.incarnation_generation) fail(409, 'Operation belongs to a retired machine incarnation.');
    if (verb === 'start' && record?.cloud?.fenced) fail(409, 'Machine is fenced and cannot start on this host.');
    const existing = record?.cloud?.operations?.[key];
    if (existing) {
      if (existing.fingerprint !== fingerprint) fail(409, 'Operation ID was already used with a different payload.');
      if (existing.result) return existing.result;
      if (body.generation !== record.cloud.generation) fail(409, 'Pending operation was superseded.');
    } else {
      if (verb === 'create' && body.image_ref !== this.imageRef) fail(409, 'Requested image is not installed on this host.');
      if (record && !record.cloud) fail(409, 'ID belongs to a local machine.');
      let reincarnating = false;
      if (verb === 'create' && record) {
        const removedDisk = Object.values(record.cloud.operations || {}).some(operation => {
          if (operation.verb !== 'destroy' || !operation.result) return false;
          const payload = JSON.parse(operation.fingerprint);
          return payload.generation === record.cloud.generation && payload.delete_disk === true
            && operation.result.body?.data?.deleted === true && operation.result.body?.data?.disk_id === null;
        });
        if (body.recreate !== true || record.cloud.deleted !== true || !removedDisk || body.generation <= record.cloud.generation) fail(409, 'Machine ID is already reserved; re-creation requires confirmed disk deletion and a higher generation.');
        const held = await this.runtime.list();
        if (Object.hasOwn(held, id)) fail(409, 'Retired machine is still held by the runtime.');
        try {
          await this.runtime.describe(id);
          fail(409, 'Retired machine still exists in the runtime.');
        } catch (error) {
          if (error.status !== 404) throw error;
        }
        reincarnating = true;
      }
      if (verb !== 'create' && (!record || record.cloud.deleted)) fail(404, 'No such managed machine.', 'machine_not_found');
      if (record && body.generation < record.cloud.generation) fail(409, 'Stale boot generation.');
      if (record && verb === 'start' && body.generation === record.cloud.generation) {
        // A generation identifies one boot, not every lifecycle command. The
        // initial start shares create's generation; a stopped boot cannot be
        // resurrected by a new command with that same generation.
        const priorIntent = Object.values(record.cloud.operations).some(operation => {
          const payload = JSON.parse(operation.fingerprint);
          return payload.generation === body.generation && operation.verb !== 'create'
            && !Object.hasOwn(payload, 'image_ref');
        });
        if (priorIntent) fail(409, 'A new start requires a new boot generation.');
      }
      // Do not let a later command overtake an ambiguous runtime call. The CP
      // must retry/reconcile that intent first, including after a process crash.
      if (record && Object.values(record.cloud.operations).some(operation => !operation.result)) fail(409, 'A pending operation must be reconciled first.');
      if (!record || reincarnating) {
        const previousOperations = record?.cloud?.operations || {};
        const held = await this.runtime.list();
        if (Object.hasOwn(held, id)) fail(409, 'ID is already held by the runtime.');
        record = {
          id, ...spec, created_at: new Date().toISOString(), registration_token: randomUUID().replaceAll('-', ''),
          authorized_keys: [this.publicKey], machine_token_hash: null, boot_id: null,
          capabilities: null, last_heartbeat_at: null, desired_state: 'stopped',
          cloud: { image_ref: body.image_ref, operations: previousOperations, create_spec: spec,
            ...(reincarnating ? { incarnation_generation: body.generation } : {}) },
        };
        this.registry.records[id] = record;
      }
      record.cloud.generation = body.generation;
      record.runtime_generation = body.generation;
      record.cloud.operations[key] = { fingerprint, verb, pending_at: new Date().toISOString() };
      this.registry.flush();
    }
    const operation = record.cloud.operations[key];
    if (verb !== 'create') { revokeDesktop(id); revokeSsh(id); }
    if (verb === 'create') {
      if (record.cloud.image_ref !== this.imageRef) fail(409, 'Pending create requires its original installed image.');
      await this.runtime.create({ computer_id: id, ...record.cloud.create_spec,
        registration_token: record.registration_token, authorized_keys: record.authorized_keys });
    } else if (verb === 'start') {
      const described = await this.runtime.describe(id);
      if (described.status === 'unknown') fail(409, 'Machine state is uncertain.');
      if (!operation.boot_prepared && described.status === 'stopped') {
        record.cloud.previous_boot_ids = [...new Set([...(record.cloud.previous_boot_ids || []), record.boot_id].filter(Boolean))];
        record.cloud.boot_requested_at = new Date().toISOString();
        record.last_heartbeat_at = null;
        record.capabilities = null;
        record.boot_id = null;
        operation.boot_prepared = true;
      }
      record.desired_state = 'running';
      this.registry.flush();
      try {
        await this.runtime.startMachine(id);
      } catch (error) {
        // The native runner checks these limits before it writes launch intent
        // or starts QEMU. They are known refusals rather than ambiguous runtime
        // outcomes, so settle the journal entry and let later lifecycle work
        // (especially destroy) proceed. Unknown transport and runtime failures
        // must remain pending for an exact retry.
        if (error.status !== 422 || ![
          'Native host memory limit reached',
          'Native host running-computer limit reached',
        ].includes(error.message)) throw error;
        record.desired_state = 'stopped';
        const result = { status: error.status, body: { error: error.message } };
        operation.result = result;
        this.registry.flush();
        return result;
      }
    } else if (verb === 'shutdown' || verb === 'force-stop') {
      this.registry.update(id, { desired_state: 'stopped' });
      if (verb === 'shutdown') await this.runtime.shutdown(id);
      else await this.runtime.forceStop(id);
    } else {
      this.registry.update(id, { desired_state: 'stopped' });
      // destroy is itself idempotent and refuses a live machine. Never suppress
      // an unavailable describe into a claim that the disk has been removed.
      await this.runtime.settle(id, { strict: true });
      await this.runtime.destroy(id, body.delete_disk);
      record.cloud.deleted = true;
      record.registration_token = null;
      record.machine_token_hash = null;
    }
    const data = verb === 'destroy'
      ? { id, status: 'stopped', disk_id: body.delete_disk ? null : id, generation: body.generation, ready: false, deleted: true, boot_id: null, capabilities: null, last_heartbeat_at: null }
      : await this.describe(record, key);
    const result = { status: verb === 'create' ? 201 : 200, body: { data } };
    operation.result = result;
    this.registry.flush();
    return result;
  }
}
