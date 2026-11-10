#!/usr/bin/env node
import { inspectHost } from '../src/preflight.js';
import { createServer } from '../src/server.js';
import { stateDir } from '../src/paths.js';
import { ensurePython } from '../src/python.js';
import { downloadImage, imageStatus, manifestUrl } from '../src/image.js';

/**
 * Normalize Node's os.arch() to the QEMU/manifest architecture names.
 * darwin is always aarch64 (Apple Silicon), linux x64 becomes x86_64.
 */
function normalizeArchitecture(platform, arch) {
  if (platform === 'darwin') return 'aarch64';
  if (arch === 'x64') return 'x86_64';
  if (arch === 'arm64') return 'aarch64';
  return arch;
}

const command = process.argv[2] ?? 'start';
const port = Number(
  process.env.MOLA_PORT
  || process.argv.find((argument) => argument.startsWith('--port='))?.split('=')[1]
  || 4141,
);

const ESC = '[';
const bold = (s) => `${ESC}1m${s}${ESC}0m`;
const dim = (s) => `${ESC}2m${s}${ESC}0m`;
const green = (s) => `${ESC}32m${s}${ESC}0m`;
const red = (s) => `${ESC}31m${s}${ESC}0m`;

function reportHost(host) {
  const mark = (ok) => (ok ? green('yes') : red('no'));
  console.log(`  platform      ${host.platform}/${host.arch}`);
  console.log(`  python3       ${mark(host.python)}`);
  console.log(`  ssh           ${mark(host.ssh)}`);
  console.log(`  docker        ${mark(host.docker)} ${dim('(prepares images; does not run the guest)')}`);
  console.log(`  accelerator   ${host.accelerator ? green(host.accelerator) : red('none')}`);
  if (host.qemu) console.log(`  qemu          ${dim(host.qemu)}`);
  if (host.qemu) {
    console.log(`  graphics      ${host.acceleratedGraphics ? green('accelerated (virgl)') : 'software (llvmpipe)'}`);
  }
  console.log(`  guest image   ${host.image ? green('yes') : red('none')} ${dim(host.imagePath)}`);
}

/**
 * A missing image is not the same failure as a missing hypervisor.
 * The host is fine; there is simply nothing to clone yet, so say how to fix it.
 */
function explainMissingImage(host) {
  const home = host.imagePath.replace(/\/image$/, '');
  console.log(`\n${red('No guest image.')} Machines are cloned from one, and you build it once.\n`);
  console.log('  That step needs Docker running, and on Apple Silicon a copy of Try');
  console.log('  Omarchy in /Applications to take the base filesystem from. Neither is');
  console.log('  used again once the image exists.\n');
  console.log(dim('    git clone https://github.com/obaid/mola-core'));
  console.log(dim('    cd mola-core && npm install'));
  console.log(dim(`    python3 bin/native-prepare --output ${home}`));
  console.log(`\n  ${dim('https://mola.sh/#image')}\n`);
}

if (command === 'doctor') {
  const host = inspectHost();
  console.log(bold('\nMola engine — host check\n'));
  reportHost(host);
  if (!host.hostReady) {
    console.log(`\n${red('Not ready.')} ${host.reason}\n`);
    process.exit(1);
  }
  if (!host.image) {
    explainMissingImage(host);
    process.exit(1);
  }
  console.log(`\n${green('Ready.')}\n`);
  process.exit(0);
}

if (command === 'mcp') {
  // stdout belongs to the JSON-RPC stream from here on. The host check is
  // deliberately skipped: this process talks to an engine over HTTP and does not
  // care whether QEMU exists here. Reporting on a missing hypervisor would only
  // arrive as a crash inside somebody's editor.
  const { runMcpServer } = await import('../src/mcp/index.js');
  await runMcpServer();
  process.exit(0);
}

if (command === 'uninstall') {
  const { uninstall } = await import('../src/uninstall.js');
  const result = await uninstall({
    yes: process.argv.includes('--yes') || process.argv.includes('-y'),
    keepImage: process.argv.includes('--keep-image'),
    colour: { bold, dim, red, green },
  });
  process.exit(result.removed ? 0 : 1);
}

if (command !== 'start') {
  console.log('usage: mola [start|doctor|mcp|uninstall] [--port=4141]');
  console.log('       mola start [--refresh-image]');
  console.log('       mola uninstall [--yes] [--keep-image]');
  process.exit(1);
}

const host = inspectHost();
if (!host.hostReady) {
  console.log(bold('\nMola engine\n'));
  reportHost(host);
  console.log(`\n${red('Cannot start.')} ${host.reason}\n`);
  process.exit(1);
}
const refresh = process.argv.includes('--refresh-image');

/**
 * Say when the guest image on disk is behind the published one.
 *
 * A cached image is never fetched again, so without this a fix to the guest
 * would reach new installations and nobody else. Deliberately a notice rather
 * than an automatic download: the image is about 1.5 GB and starting the engine
 * is not consent to fetch it.
 */
async function reportImageAge() {
  const status = await imageStatus();
  if (status.state === 'current' || status.state === 'unknown') return;
  const why = status.state === 'unrecorded'
    ? 'predates this check, so it is missing anything fixed since'
    : `is behind the published ${status.published ?? 'image'}`;
  console.log(`\n  ${dim('The guest image')} ${why}${dim('.')}`);
  console.log(`  ${dim('Machines already created keep their own disks and are unaffected.')}`);
  console.log(`  ${dim('Refresh it with')} mola start --refresh-image\n`);
}

const hostArch = normalizeArchitecture(host.platform, host.arch);

if (host.image && refresh) {
  console.log(bold('\nMola engine\n'));
  console.log('  refreshing the guest image\n');
  try {
    await downloadImage({ hostArch });
  } catch (error) {
    if (error.code === 'ARCH_MISMATCH') {
      console.log(`\n${red('Architecture mismatch.')}\n`);
      console.log(`  ${error.message}\n`);
    } else {
      console.log(`\n${red('Could not fetch the guest image.')} ${error.message}\n`);
    }
    console.log(`  The image already on disk is untouched.\n`);
    process.exit(1);
  }
} else if (host.image) {
  await reportImageAge();
}

if (!host.image) {
  console.log(bold('\nMola engine\n'));
  reportHost(host);
  // Fetch it rather than asking the operator to learn a second command. The
  // build path stays available for anyone who wants to make their own.
  console.log('');
  try {
    await downloadImage({ hostArch });
  } catch (error) {
    if (error.code === 'NO_MANIFEST') {
      console.log(`  ${dim('no prebuilt image available:')} ${dim(manifestUrl())}`);
      explainMissingImage(host);
    } else if (error.code === 'ARCH_MISMATCH') {
      console.log(`\n${red('Architecture mismatch.')}\n`);
      console.log(`  ${error.message}\n`);
      console.log(`  Build a compatible image yourself: ${dim('https://mola.sh/#image')}\n`);
    } else {
      console.log(`\n${red('Could not fetch the guest image.')} ${error.message}\n`);
      console.log(`  Retry, or build one yourself: ${dim('https://mola.sh/#image')}\n`);
    }
    process.exit(1);
  }
}

process.env.MOLA_PORT = String(port);
ensurePython();
const { server, runtime, token } = await createServer({ host, port });

server.listen(port, '127.0.0.1', () => {
  const base = `http://127.0.0.1:${port}`;
  console.log(`
${bold('  Mola')} ${dim('· Omarchy computers on this machine')}

  ${bold('API')}    ${green(`${base}/v1`)}
  ${bold('Token')}  ${token}
  ${dim(`State  ${stateDir()}`)}

  ${dim('Give your agent these:')}

    curl ${base}/v1 -H "Authorization: Bearer $TOKEN"

    POST   /v1/machines                  create a fresh Omarchy computer
    GET    /v1/machines                  list them
    GET    /v1/machines/{id}             describe one
    POST   /v1/machines/{id}/start       start it
    POST   /v1/machines/{id}/stop        shut down   {"force": true} cuts power
    POST   /v1/machines/{id}/actions     exec | read_file | write_file | screenshot
                                         click | move | scroll | type | key
    POST   /v1/machines/{id}/desktop     a browser URL for the desktop
    DELETE /v1/machines/{id}             destroy it and its disk

  ${dim('Ctrl-C to stop. Machines keep their disks; the engine does not.')}
`);
});

const shutdown = () => {
  console.log('\n  stopping...');
  server.close();
  runtime.stop();
  process.exit(0);
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
