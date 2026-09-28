import { readFileSync } from 'node:fs';
import { runtimeScript } from './paths.js';
import { hostDescription } from './host-api.js';

const INSTALLER = Buffer.from(readFileSync(runtimeScript('cua-install.sh'))).toString('base64');

/** Converge pre-Cua cloud disks once per boot, outside the heartbeat response. */
export class CuaRollout {
  constructor(hostApi) {
    this.api = hostApi;
    this.attempts = new Map();
  }

  schedule(record) {
    if (!record?.cloud || record.cloud.deleted || !record.boot_id
      || !record.capabilities?.shell || !record.capabilities?.display || !record.capabilities?.sshd
      || record.capabilities?.cua?.installed === true) return;
    const old = this.attempts.get(record.id);
    if (old?.bootId === record.boot_id && (old.done || old.running || old.retryAt > Date.now())) return;
    const attempt = { bootId: record.boot_id, running: true, done: false, retryAt: 0 };
    this.attempts.set(record.id, attempt);
    void this.api.locked(record.id, async () => {
      const latest = this.api.record(record.id);
      if (latest.boot_id !== attempt.bootId) return;
      const target = await this.api.runtime.describe(record.id);
      if (!hostDescription(latest, target).ready) return;
      const result = await this.api.action({ id: record.id, ...target }, {
        action: 'exec',
        command: `printf '%s' '${INSTALLER}' | base64 -d | /bin/bash`,
        timeout: 120,
      });
      if (result.exit_code !== 0 || result.timed_out) {
        throw new Error('Managed Cua installation failed on a retained disk.');
      }
      attempt.done = true;
    }).catch(error => {
      attempt.retryAt = Date.now() + 5 * 60_000;
      console.warn(`[cua] ${record.id}: ${error.message}`);
    }).finally(() => { attempt.running = false; });
  }
}
