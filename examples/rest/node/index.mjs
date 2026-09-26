import { writeFile } from 'node:fs/promises';
import { MolaClient } from '../../lib/mola-client.mjs';

const client = new MolaClient();
const keep = process.argv.includes('--keep');
let machine;
try {
  machine = await client.createMachine({ name: 'rest-example', vcpus: 1, memory_mb: 2048, disk_gb: 20 });
  machine = await client.waitForStatus(machine.id, 'ready');
  const action = payload => client.action(machine.id, payload);
  const uname = await action({ action: 'exec', command: 'uname -a' });
  await action({ action: 'write_file', path: '~/mola-example.txt', content: 'hello from Mola\n' });
  const roundTrip = await action({ action: 'read_file', path: '~/mola-example.txt' });
  const screenshot = await action({ action: 'screenshot' });
  const screenshotPath = new URL('./mola-example.png', import.meta.url);
  await writeFile(screenshotPath, Buffer.from(screenshot.image_base64, 'base64'));
  console.log({ id: machine.id, uname: uname.stdout.trim(), file: Buffer.from(roundTrip.content_base64, 'base64').toString(), screenshot: screenshotPath.pathname });
  await client.stopMachine(machine.id);
  await client.waitForStatus(machine.id, 'stopped');
  console.log(`Stopped ${machine.id}. Start it later to keep working with the same disk.`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  if (machine && !keep) {
    try { await client.deleteMachine(machine.id); console.log(`Deleted disposable machine ${machine.id}.`); }
    catch (error) { console.error(`Cleanup failed for ${machine.id}: ${error.message}`); process.exitCode = 1; }
  }
}
