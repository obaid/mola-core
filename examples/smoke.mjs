import { MolaClient } from './lib/mola-client.mjs';

const client = new MolaClient();
const keep = process.argv.includes('--keep');
let machine;
try {
  machine = await client.createMachine({ name: `examples-smoke-${Date.now().toString(36)}`, vcpus: 1, memory_mb: 2048, disk_gb: 20 });
  await client.waitForStatus(machine.id, 'ready', { timeoutMs: 240_000 });
  const shell = await client.action(machine.id, { action: 'exec', command: 'printf smoke-ok' });
  if (shell.stdout !== 'smoke-ok') throw new Error('REST shell assertion failed.');
  await client.action(machine.id, { action: 'write_file', path: '~/smoke.txt', content: 'rest-ok\n' });
  const session = await client.actionSession(machine.id);
  try {
    for (let index = 0; index < 20; index += 1) {
      const result = await session.action({ action: 'exec', command: 'true' });
      if (result.exit_code !== 0) throw new Error(`Action-session request ${index} failed.`);
    }
    const expected = Buffer.from('binary-ok\n');
    await session.action({ action: 'write_file', path: '~/smoke.bin' }, { data: expected });
    const actual = await session.action({ action: 'read_file', path: '~/smoke.bin' }, { binary: true });
    if (!actual.data.equals(expected)) throw new Error('Binary action-session assertion failed.');
    const screenshot = await session.action({ action: 'screenshot' }, { binary: true });
    if (!screenshot.data.length) throw new Error('Binary screenshot was empty.');
  } finally {
    session.close();
  }
  console.log(JSON.stringify({ success: true, machine_id: machine.id, checks: ['rest', '20 ordered session actions', 'binary write/read', 'binary screenshot'] }, null, 2));
} finally {
  if (machine && !keep) await client.deleteMachine(machine.id);
}
