import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { tmpdir } from 'node:os';
import { SnapshotTransfer } from '../src/snapshot-transfer.js';

const sha = value => createHash('sha256').update(value).digest('hex');
const bytes = Buffer.from('direct snapshot bytes');

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'mola-direct-snapshot-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const machineId = randomUUID(), snapshotId = randomUUID();
  const folder = join(root, machineId, snapshotId);
  mkdirSync(folder, { recursive: true });
  writeFileSync(join(folder, 'disk.gz'), bytes);
  const manifest = { id: snapshotId, artifact_bytes: bytes.length, artifact_sha256: sha(bytes) };
  return { root, folder, machineId, snapshotId, manifest };
}

const uploadGrant = manifest => ({
  version: 1, transfer_id: 'upload-1', artifact_bytes: manifest.artifact_bytes,
  artifact_sha256: manifest.artifact_sha256, concurrency: 2, max_seconds: 60,
  parts: [
    { number: 1, offset: 0, size: 8, url: 'https://account.r2.cloudflarestorage.com/bucket/object?partNumber=1', headers: {} },
    { number: 2, offset: 8, size: bytes.length - 8, url: 'https://account.r2.cloudflarestorage.com/bucket/object?partNumber=2', headers: {} },
  ],
});

test('uploads snapshot parts directly, persists receipts and skips acknowledged parts on retry', async t => {
  const f = fixture(t), attempts = [];
  let failSecond = true;
  const fetcher = async (url, options) => {
    const part = Number(new URL(url).searchParams.get('partNumber'));
    attempts.push(part);
    const chunks = [];
    for await (const chunk of Readable.from(options.body)) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    assert.deepEqual(body, part === 1 ? bytes.subarray(0, 8) : bytes.subarray(8));
    if (part === 2 && failSecond) { failSecond = false; return new Response('', { status: 503 }); }
    return new Response('', { status: 200, headers: { etag: `"part-${part}"` } });
  };
  const transfer = new SnapshotTransfer({ root: f.root, fetcher });
  await assert.rejects(transfer.upload(f.machineId, f.snapshotId, f.manifest, uploadGrant(f.manifest)), /part 2/);
  const result = await transfer.upload(f.machineId, f.snapshotId, f.manifest, uploadGrant(f.manifest));
  assert.equal(result.complete, true);
  assert.deepEqual(result.parts, [
    { part_number: 1, etag: '"part-1"' },
    { part_number: 2, etag: '"part-2"' },
  ]);
  assert.deepEqual(attempts.sort(), [1, 2, 2]);
});

test('downloads and verifies a direct archive before publishing it locally', async t => {
  const f = fixture(t);
  rmSync(join(f.folder, 'disk.gz'));
  const fetcher = async (_url, options) => {
    assert.equal(options.headers['x-amz-server-side-encryption-customer-algorithm'], 'AES256');
    return new Response(bytes, { status: 200 });
  };
  const transfer = new SnapshotTransfer({ root: f.root, fetcher });
  const result = await transfer.download(f.machineId, f.snapshotId, f.manifest, {
    version: 1, transfer_id: 'download-1', artifact_bytes: bytes.length, artifact_sha256: sha(bytes),
    url: 'https://account.r2.cloudflarestorage.com/bucket/object?signature=test',
    headers: { 'x-amz-server-side-encryption-customer-algorithm': 'AES256' }, max_seconds: 60,
  });
  assert.deepEqual(result, { complete: true, downloaded_bytes: bytes.length });
  assert.deepEqual(readFileSync(join(f.folder, 'disk.gz')), bytes);
});

test('rejects transfer grants that could reach a non-R2 service', async t => {
  const f = fixture(t);
  const grant = uploadGrant(f.manifest);
  grant.parts[0].url = 'http://127.0.0.1/latest/meta-data';
  const transfer = new SnapshotTransfer({ root: f.root, fetcher: () => assert.fail('unsafe request was sent') });
  await assert.rejects(transfer.upload(f.machineId, f.snapshotId, f.manifest, grant), /not an R2 S3 endpoint/);
});
