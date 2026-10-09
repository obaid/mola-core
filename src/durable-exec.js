import { createHash, createHmac, randomBytes, randomUUID } from 'node:crypto';
import { constants, existsSync, mkdirSync, openSync, closeSync, fstatSync, fsyncSync, readFileSync, linkSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { statePath } from './paths.js';

const fail = (status, code) => { throw Object.assign(new Error(code.replaceAll('_', ' ')), { status, code }); };
const check = (condition, code = 'invalid_exec_arguments', status = 400) => { if (!condition) fail(status, code); };
const hash = value => createHash('sha256').update(value).digest('hex');
const canonical = value => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
};
const TERMINAL = new Set(['completed', 'failed']);
const STATUSES = new Set(['queued', 'running', 'completed', 'failed', 'outcome_unknown']);

function binding(body) {
  check(typeof body?.operation_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.operation_id));
  check(Number.isSafeInteger(body.expected_generation) && body.expected_generation >= 1);
  check(typeof body.expected_boot_id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(body.expected_boot_id));
  return { operation_id: body.operation_id.toLowerCase(), generation: body.expected_generation, boot_id: body.expected_boot_id };
}
function definition(body) {
  check(typeof body.command === 'string' && Buffer.byteLength(body.command) > 0 && Buffer.byteLength(body.command) <= 32768 && !body.command.includes('\0'));
  check(Number.isInteger(body.timeout_seconds) && body.timeout_seconds >= 1 && body.timeout_seconds <= 900);
  check(body.payload !== null && typeof body.payload === 'object' && !Array.isArray(body.payload) && Buffer.byteLength(JSON.stringify(body.payload)) <= 1048576);
  check(Array.isArray(body.redact) && body.redact.length <= 20 && body.redact.every(value => typeof value === 'string' && Buffer.byteLength(value) >= 4 && Buffer.byteLength(value) <= 4096));
  return { command: body.command, timeout_seconds: body.timeout_seconds, payload: body.payload, redact: body.redact };
}
function safeReceipt(data, record) {
  check(data && typeof data === 'object' && data.operation_id === record.operation_id && data.generation === record.generation
    && data.boot_id === record.boot_id && STATUSES.has(data.status), 'invalid_exec_receipt', 502);
  const terminal = TERMINAL.has(data.status);
  check(data.terminal === terminal, 'invalid_exec_receipt', 502);
  const result = { operation_id: record.operation_id, generation: record.generation, boot_id: record.boot_id, status: data.status, terminal };
  if (terminal) {
    check(data.result && typeof data.result === 'object' && Number.isInteger(data.result.exit_code) && data.result.exit_code >= -255 && data.result.exit_code <= 255
      && typeof data.result.stdout === 'string' && Buffer.byteLength(data.result.stdout) <= 65536, 'invalid_exec_receipt', 502);
    check(data.result.stderr === undefined || typeof data.result.stderr === 'string' && Buffer.byteLength(data.result.stderr) + Buffer.byteLength(data.result.stdout) <= 65536, 'invalid_exec_receipt', 502);
    check(data.result.timed_out === undefined || typeof data.result.timed_out === 'boolean', 'invalid_exec_receipt', 502);
    check(data.result.cancelled === undefined || typeof data.result.cancelled === 'boolean', 'invalid_exec_receipt', 502);
    check(data.status === 'completed' ? data.result.exit_code === 0 && data.result.timed_out !== true && data.result.cancelled !== true : data.result.exit_code !== 0, 'invalid_exec_receipt', 502);
    result.result = { exit_code: data.result.exit_code, stdout: data.result.stdout, stderr: data.result.stderr ?? '',
      ...(data.result.timed_out === undefined ? {} : { timed_out: data.result.timed_out }),
      ...(data.result.cancelled === undefined ? {} : { cancelled: data.result.cancelled }) };
  }
  return result;
}

/** Host-only metadata journal. Payloads/commands/redaction secrets are never
 * written here. Its HMAC key lives outside all guest disks and checkpoints. */
export class HostDurableExec {
  constructor({ root = null } = {}) { this.root = root; }
  directory() {
    const path = this.root ?? statePath('job-executions');
    mkdirSync(path, { recursive: true, mode: 0o700 });
    const descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    try { const info = fstatSync(descriptor); check(info.isDirectory() && !(info.mode & 0o077), 'unsafe_exec_journal', 500); }
    finally { closeSync(descriptor); }
    return path;
  }
  read(path) {
    let descriptor;
    try { descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0)); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
    try {
      const info = fstatSync(descriptor); check(info.isFile() && !(info.mode & 0o077) && info.size <= 262144, 'unsafe_exec_journal', 500);
      return JSON.parse(readFileSync(descriptor, 'utf8'));
    } finally { closeSync(descriptor); }
  }
  save(path, value, exclusive = false) {
    const temporary = `${path}.${randomUUID()}.new`; let descriptor;
    try {
      descriptor = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0), 0o600);
      writeFileSync(descriptor, JSON.stringify(value)); fsyncSync(descriptor); closeSync(descriptor); descriptor = undefined;
      if (exclusive) {
        try { linkSync(temporary, path); } catch (error) { if (error.code === 'EEXIST') return false; throw error; }
        unlinkSync(temporary);
      } else renameSync(temporary, path);
      const directory = openSync(dirname(path), constants.O_RDONLY); try { fsyncSync(directory); } finally { closeSync(directory); }
      return true;
    } finally { if (descriptor !== undefined) closeSync(descriptor); if (existsSync(temporary)) unlinkSync(temporary); }
  }
  key() {
    const path = join(this.directory(), 'fingerprint.key');
    const temporary = `${path}.${randomUUID()}.new`;
    let candidate;
    try {
      candidate = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0), 0o600);
      writeFileSync(candidate, randomBytes(32)); fsyncSync(candidate); closeSync(candidate); candidate = undefined;
      try { linkSync(temporary, path); } catch (error) { if (error.code !== 'EEXIST') throw error; }
      const directory = openSync(dirname(path), constants.O_RDONLY); try { fsyncSync(directory); } finally { closeSync(directory); }
    } finally { if (candidate !== undefined) closeSync(candidate); if (existsSync(temporary)) unlinkSync(temporary); }
    const descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    try { const info = fstatSync(descriptor); check(info.isFile() && !(info.mode & 0o077) && info.size === 32, 'unsafe_exec_key', 500); return readFileSync(descriptor); }
    finally { closeSync(descriptor); }
  }
  path(id, operation) { return join(this.directory(), `${hash(`${id}|${operation}`)}.json`); }
  digest(id, request, content) { return createHmac('sha256', this.key()).update('mola-durable-exec-v1\0').update(canonical({ machine_id: id, ...request, ...content })).digest('hex'); }
  unknown(record) { return { operation_id: record.operation_id, generation: record.generation, boot_id: record.boot_id, status: 'outcome_unknown', terminal: false }; }
  retain(path, record, receipt) {
    if (receipt.terminal) this.save(`${path}.terminal`, { ...record, receipt }, true);
    const terminal = this.read(`${path}.terminal`);
    if (terminal) return safeReceipt(terminal.receipt, record);
    record.receipt = receipt; this.save(path, record); return receipt;
  }
  async existing(api, id, path, record) {
    const prior = this.read(`${path}.terminal`) ?? this.read(path);
    check(prior && prior.operation_id === record.operation_id && prior.machine_id === id && prior.generation === record.generation && prior.boot_id === record.boot_id
      && prior.payload_digest === record.payload_digest, 'exec_payload_mismatch', 409);
    return this.reconcile(api, id, prior, path);
  }
  async target(api, id, request) {
    const record = api.registry.get(id);
    if (!record?.cloud || record.cloud.deleted || record.cloud.generation !== request.generation || record.boot_id !== request.boot_id) return null;
    try {
      if (!(await api.describe(record)).ready) return null;
      return { record, target: await api.runtime.describe(id) };
    } catch { return null; }
  }
  async reconcile(api, id, record, path, cancel = false) {
    if (record.receipt?.terminal === true) return safeReceipt(record.receipt, record);
    const current = await this.target(api, id, record);
    if (!current) return this.unknown(record);
    try {
      const data = await api.guestTools.run(id, current.target, 'job-exec', {
        tool: cancel ? 'exec_cancel' : 'exec_status', arguments: { operation_id: record.operation_id, expected_generation: record.generation, expected_boot_id: record.boot_id },
      }, { generation: record.generation, boot_id: record.boot_id });
      const receipt = safeReceipt(data, record);
      if (receipt.terminal) return this.retain(path, record, receipt);
      if (current.record.cloud.generation !== record.generation || current.record.boot_id !== record.boot_id || current.record.cloud.deleted) return this.unknown(record);
      return this.retain(path, record, receipt);
    } catch { return this.unknown(record); }
  }
  async handle(api, id, mode, body) {
    const request = binding(body), path = this.path(id, request.operation_id), prior = this.read(`${path}.terminal`) ?? this.read(path);
    if (prior) check(prior.operation_id === request.operation_id && prior.machine_id === id && prior.generation === request.generation && prior.boot_id === request.boot_id, 'exec_binding_mismatch', 409);
    if (mode !== 'submit') {
      check(prior, 'exec_operation_not_found', 404);
      return this.reconcile(api, id, prior, path, mode === 'cancel');
    }
    const content = definition(body), digest = this.digest(id, request, content);
    if (prior) {
      check(prior.payload_digest === digest, 'exec_payload_mismatch', 409);
      // Even an accepted request with no guest receipt is uncertainty. Never
      // submit again after a response loss, process restart or elapsed time.
      return this.reconcile(api, id, prior, path);
    }
    const current = await this.target(api, id, request);
    check(current, 'exec_generation_unavailable', 409);
    const record = { machine_id: id, ...request, payload_digest: digest, accepted_at: new Date().toISOString() };
    // A readonly scope-capability refusal is proof no command was submitted.
    let supported = false;
    try {
      const capability = await api.guestTools.run(id, current.target, 'job-exec', { tool: 'exec_capabilities', arguments: {} }, { generation: request.generation, boot_id: request.boot_id });
      supported = capability?.supported === true && capability?.max_timeout_seconds === 900;
    } catch { /* No exec submission occurred. */ }
    if (!supported) {
      record.receipt = { ...request, status: 'failed', terminal: true, result: { exit_code: 125, stdout: '', stderr: '' } };
      if (!this.save(path, record, true)) return this.existing(api, id, path, record);
      return this.retain(path, record, record.receipt);
    }
    check(current.record.cloud.generation === request.generation && current.record.boot_id === request.boot_id && !current.record.cloud.deleted, 'exec_generation_unavailable', 409);
    if (!this.save(path, record, true)) return this.existing(api, id, path, record);
    try {
      const data = await api.guestTools.run(id, current.target, 'job-exec', { tool: 'exec_submit', arguments: {
        operation_id: request.operation_id, expected_generation: request.generation, expected_boot_id: request.boot_id, ...content, payload_digest: digest,
      } }, { generation: request.generation, boot_id: request.boot_id });
      const receipt = safeReceipt(data, record);
      if (receipt.terminal) return this.retain(path, record, receipt);
      if (current.record.cloud.generation !== request.generation || current.record.boot_id !== request.boot_id || current.record.cloud.deleted) return this.unknown(record);
      return this.retain(path, record, receipt);
    } catch { return this.unknown(record); }
  }
}
