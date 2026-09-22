import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.MOLA_HOME = mkdtempSync(join(tmpdir(), 'mola-test-'));

const { presentStatus, validateSpec, validateAction } = await import('../src/api.js');
const { Registry } = await import('../src/state.js');
const { GuestService } = await import('../src/guest.js');
const { manifestUrl, hasImage, imageDir } = await import('../src/image.js');

test('a running machine is not ready until the guest reports in', () => {
  assert.equal(presentStatus('running', {}), 'booting');
  assert.equal(presentStatus('running', { last_heartbeat_at: new Date().toISOString(), capabilities: { shell: true } }), 'ready');
});

test('a stale heartbeat does not keep a machine ready', () => {
  const old = new Date(Date.now() - 120_000).toISOString();
  assert.equal(presentStatus('running', { last_heartbeat_at: old, capabilities: { shell: true } }), 'booting');
});

test('unknown is reported, never flattened into stopped', () => {
  // A provider that cannot see a machine has not said the machine stopped.
  // Collapsing the two is how a platform loses one it is still running.
  assert.equal(presentStatus('unknown', {}), 'unknown');
  assert.equal(presentStatus('stopped', {}), 'stopped');
});

test('specs are bounded', () => {
  assert.deepEqual(validateSpec({}).vcpus, 4);
  assert.equal(validateSpec({ name: '  box  ' }).name, 'box');
  assert.throws(() => validateSpec({ vcpus: 99 }), /vcpus/);
  assert.throws(() => validateSpec({ memory_mb: 128 }), /memory_mb/);
  assert.throws(() => validateSpec({ disk_gb: 4 }), /disk_gb/);
});

test('machine defaults can be sized for a constrained host', () => {
  const before = {
    vcpus: process.env.MOLA_DEFAULT_VCPUS,
    memory: process.env.MOLA_DEFAULT_MEMORY_MB,
    disk: process.env.MOLA_DEFAULT_DISK_GB,
  };
  process.env.MOLA_DEFAULT_VCPUS = '1';
  process.env.MOLA_DEFAULT_MEMORY_MB = '2048';
  process.env.MOLA_DEFAULT_DISK_GB = '20';
  try {
    assert.deepEqual(validateSpec({ name: 'lean' }), {
      name: 'lean',
      vcpus: 1,
      memory_mb: 2048,
      disk_gb: 20,
    });
    assert.deepEqual(validateSpec({ name: 'explicit', vcpus: 2, memory_mb: 3072, disk_gb: 24 }), {
      name: 'explicit',
      vcpus: 2,
      memory_mb: 3072,
      disk_gb: 24,
    });
    process.env.MOLA_DEFAULT_VCPUS = 'many';
    assert.throws(() => validateSpec({}), /MOLA_DEFAULT_VCPUS must be an integer/);
  } finally {
    for (const [key, value] of [
      ['MOLA_DEFAULT_VCPUS', before.vcpus],
      ['MOLA_DEFAULT_MEMORY_MB', before.memory],
      ['MOLA_DEFAULT_DISK_GB', before.disk],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test('only known actions are accepted', () => {
  assert.throws(() => validateAction({ action: 'rm -rf' }), /action must be one of/);
  assert.throws(() => validateAction({ action: 'exec' }), /command/);
  assert.throws(() => validateAction({ action: 'write_file', path: '~/a' }), /content/);
  assert.doesNotThrow(() => validateAction({ action: 'exec', command: 'ls' }));
  assert.doesNotThrow(() => validateAction({ action: 'screenshot' }));
});

test('a registration token cannot be redeemed twice', () => {
  const registry = new Registry(join(process.env.MOLA_HOME, 'a.json'));
  const guests = new GuestService(registry);
  const record = registry.create({ name: 'x', vcpus: 1, memory_mb: 1024, disk_gb: 16 });

  const first = guests.register({ registration_token: record.registration_token });
  assert.equal(first.status, 200);
  assert.ok(first.body.machine_token);

  // A replayed token must not mint a second credential.
  const replay = guests.register({ registration_token: record.registration_token });
  assert.equal(replay.status, 401);
});

test('an enrolment key lets a lost response be recovered', () => {
  const registry = new Registry(join(process.env.MOLA_HOME, 'b.json'));
  const guests = new GuestService(registry);
  const record = registry.create({ name: 'x', vcpus: 1, memory_mb: 1024, disk_gb: 16 });

  guests.register({ registration_token: record.registration_token, enrollment_public_key: 'k' });
  const recovered = guests.register({ registration_token: record.registration_token, enrollment_public_key: 'k' });
  assert.equal(recovered.status, 200);
  assert.equal(recovered.body.recovered, true);
});

test('an unknown registration token is refused', () => {
  const registry = new Registry(join(process.env.MOLA_HOME, 'c.json'));
  const guests = new GuestService(registry);
  assert.equal(guests.register({ registration_token: 'nope' }).status, 401);
  assert.equal(guests.register({}).status, 401);
});

test('capabilities are recorded as a claim, and the challenge rotates', () => {
  const registry = new Registry(join(process.env.MOLA_HOME, 'd.json'));
  const guests = new GuestService(registry);
  const record = registry.create({ name: 'x', vcpus: 1, memory_mb: 1024, disk_gb: 16 });
  guests.register({ registration_token: record.registration_token });

  const first = guests.heartbeat(registry.get(record.id), { capabilities: { shell: true }, boot_id: 'b1' });
  assert.equal(first.body.challenge_verified, false, 'nothing to verify on the first beat');
  assert.ok(first.body.challenge);

  const second = guests.heartbeat(registry.get(record.id), { challenge_response: first.body.challenge });
  assert.equal(second.body.challenge_verified, true);
  assert.notEqual(second.body.challenge, first.body.challenge, 'the nonce must not repeat');
});

test('the registry survives a reload', () => {
  const file = join(process.env.MOLA_HOME, 'e.json');
  const first = new Registry(file);
  const record = first.create({ name: 'keeper', vcpus: 2, memory_mb: 2048, disk_gb: 20 });

  const reopened = new Registry(file);
  assert.equal(reopened.get(record.id).name, 'keeper');

  reopened.remove(record.id);
  assert.equal(new Registry(file).get(record.id), null);
});

test('a host with no image is not ready, and says so specifically', async () => {
  const { inspectHost } = await import('../src/preflight.js');
  const host = inspectHost();
  // The test home is empty, so whatever this host can do, it has no image.
  assert.equal(hasImage(), false);
  assert.equal(host.image, false);
  assert.equal(host.ready, false, 'ready must account for the image, not just the hypervisor');
  if (host.hostReady) {
    assert.match(host.reason, /No guest image/, 'the reason names the image, not the hypervisor');
  }
});

test('the image source can be pointed elsewhere', () => {
  const original = process.env.MOLA_IMAGE_URL;
  try {
    delete process.env.MOLA_IMAGE_URL;
    assert.match(manifestUrl(), /^https:\/\//, 'there is a default to fall back on');
    process.env.MOLA_IMAGE_URL = 'file:///tmp/mine/manifest.json';
    assert.equal(manifestUrl(), 'file:///tmp/mine/manifest.json');
  } finally {
    if (original === undefined) delete process.env.MOLA_IMAGE_URL;
    else process.env.MOLA_IMAGE_URL = original;
  }
});

test('the image lives inside the state directory', () => {
  assert.ok(imageDir().startsWith(process.env.MOLA_HOME));
});

/**
 * A cached guest image is never fetched again, so a fix to the guest reaches
 * new installations and nobody else unless the engine notices it is behind.
 *
 * The version string names the Omarchy release and the architecture, which two
 * different builds of the same release share, so the comparison has to be on
 * the root filesystem's digest.
 */
test('image staleness is judged on the root filesystem, not the version string', async (t) => {
  const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');

  const home = mkdtempSync(join(tmpdir(), 'mola-image-'));
  t.after(() => {
    rmSync(home, { recursive: true, force: true });
    delete process.env.MOLA_HOME;
  });
  process.env.MOLA_HOME = home;
  mkdirSync(join(home, 'image'), { recursive: true });

  const manifest = (digest) => ({
    version: 'omarchy-4.0.1-aarch64',
    artifacts: [{ name: 'root.ext4', sha256: digest }],
  });

  const { imageStatus, installedImage } = await import(`../src/image.js?${Math.random()}`);
  const serve = (body) => {
    global.fetch = async () => new Response(JSON.stringify(body), { status: 200 });
  };
  const realFetch = global.fetch;
  t.after(() => { global.fetch = realFetch; });

  // Nothing recorded: an image installed before this check existed.
  assert.equal(installedImage(), null);
  serve(manifest('aaa'));
  assert.equal((await imageStatus()).state, 'unrecorded');

  writeFileSync(join(home, 'image', 'installed.json'), JSON.stringify(manifest('aaa')));

  serve(manifest('aaa'));
  assert.equal((await imageStatus()).state, 'current');

  // Same version, rebuilt image. This is the case a version comparison misses.
  serve(manifest('bbb'));
  const stale = await imageStatus();
  assert.equal(stale.state, 'stale');

  // A check that cannot reach the network must never block the engine.
  global.fetch = async () => { throw new Error('offline'); };
  assert.equal((await imageStatus()).state, 'unknown');
});
