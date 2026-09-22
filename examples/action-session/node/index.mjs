import { MolaClient, MolaApiError } from '../../lib/mola-client.mjs';

const client = new MolaClient();
const machine = process.env.MOLA_MACHINE;
if (!machine) throw new Error('Set MOLA_MACHINE to a ready machine id.');

const session = await client.actionSession(machine);
console.log(`Protocol ${session.capabilities.protocol}; up to ${session.capabilities.max_in_flight} queued actions.`);
try {
  const started = performance.now();
  for (let index = 0; index < 100; index += 1) await session.action({ action: 'exec', command: 'true' });
  const payload = Buffer.from('binary round trip\n');
  await session.action({ action: 'write_file', path: '~/mola-session.bin' }, { data: payload });
  const read = await session.action({ action: 'read_file', path: '~/mola-session.bin' }, { binary: true });
  if (!read.data.equals(payload)) throw new Error('Binary round trip did not match.');
  const screenshot = await session.action({ action: 'screenshot' }, { binary: true });
  console.log({ actions: 103, elapsed_ms: Math.round(performance.now() - started), file_bytes: read.data.length, screenshot_bytes: screenshot.data.length });
} catch (error) {
  if (error instanceof MolaApiError && ['stale_session', 'session_closed'].includes(error.code)) {
    console.error(`${error.message}\nRequest a fresh session with client.actionSession(machine). Never replay an action whose outcome is unknown.`);
  }
  throw error;
} finally {
  session.close();
}
