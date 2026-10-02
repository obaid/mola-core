import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { installedImages } from '../src/installed-images.js';

test('image paths and boot options come from an explicit operator catalog', t => {
  const dir = mkdtempSync(join(tmpdir(), 'mola-images-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'images.json');
  const image = { path: '/srv/mola/images/ubuntu', architecture: 'x86_64', kernel_args: 'root=/dev/vda rw', gpu: 'virtio-gpu-pci', display: 'none' };
  assert.deepEqual(installedImages({}), {});
  writeFileSync(file, JSON.stringify({ 'ubuntu-xfce:24.04-1': image }));
  assert.deepEqual(installedImages({ MOLA_IMAGES_FILE: file }), { 'ubuntu-xfce:24.04-1': image });
  for (const change of [{ path: '../guest' }, { architecture: 'arm' }, { kernel_args: null }, { display: '' }]) {
    writeFileSync(file, JSON.stringify({ ubuntu: { ...image, ...change } }));
    assert.throws(() => installedImages({ MOLA_IMAGES_FILE: file }), /Invalid installed image/);
  }
});

test('local machine API keeps the default and uses explicitly selected image defaults', async () => {
  const { validateLocalSpec } = await import('../src/api.js');
  const images = { ubuntu: { default_resources: { vcpus: 1, memory_mb: 2048, disk_gb: 20 } } };
  assert.equal(validateLocalSpec({ name: 'default' }, images).memory_mb, 4096);
  assert.deepEqual(validateLocalSpec({ name: 'ubuntu', image_ref: 'ubuntu' }, images), {
    name: 'ubuntu', image_ref: 'ubuntu', vcpus: 1, memory_mb: 2048, disk_gb: 20,
  });
  assert.equal(validateLocalSpec({ image_ref: 'ubuntu', memory_mb: 4096 }, images).memory_mb, 4096);
  assert.throws(() => validateLocalSpec({ image_ref: '/tmp/disk' }, images), /not installed/);
});
