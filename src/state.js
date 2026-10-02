import { readFileSync, writeFileSync, renameSync, existsSync, openSync, fsyncSync, closeSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { dirname } from 'node:path';
import { statePath } from './paths.js';

/**
 * The engine's own record of what exists.
 *
 * Deliberately thin. The runtime owns the truth about whether a machine is
 * running; this file only remembers the things the runtime has no opinion
 * about — the name a human gave it, and when it was made. Duplicating runtime
 * state here would create two answers to the same question.
 */
export class Registry {
  constructor(file = statePath('machines.json')) {
    this.file = file;
    this.records = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : {};
  }

  flush() {
    const temporary = `${this.file}.new`;
    writeFileSync(temporary, JSON.stringify(this.records, null, 2), { mode: 0o600 });
    const descriptor = openSync(temporary, 'r');
    try { fsyncSync(descriptor); } finally { closeSync(descriptor); }
    renameSync(temporary, this.file);
    if (process.platform !== 'win32') {
      const directory = openSync(dirname(this.file), 'r');
      try { fsyncSync(directory); } finally { closeSync(directory); }
    }
  }

  all() {
    return Object.values(this.records).sort((a, b) => a.created_at.localeCompare(b.created_at));
  }

  get(id) {
    return this.records[id] ?? null;
  }

  create({ name, vcpus, memory_mb, disk_gb, image_ref }) {
    const id = randomUUID();
    this.records[id] = {
      id,
      name,
      vcpus,
      memory_mb,
      disk_gb,
      ...(image_ref ? { image_ref } : {}),
      created_at: new Date().toISOString(),
      registration_token: randomUUID().replace(/-/g, ''),
      machine_token_hash: null,
      boot_id: null,
      capabilities: null,
      last_heartbeat_at: null,
    };
    this.flush();
    return this.records[id];
  }

  update(id, patch) {
    if (!this.records[id]) return null;
    Object.assign(this.records[id], patch);
    this.flush();
    return this.records[id];
  }

  remove(id) {
    delete this.records[id];
    this.flush();
  }
}
