// Test: Architecture validation during image download
// Added: 2026-11-10 - Fix for architecture mismatch bug where aarch64 images
// were downloaded on x86_64 hosts, causing silent hangs during boot

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { join } from 'node:path';

const testRoot = '/tmp/mola-image-arch-test';
const testPort = 9876;

describe('Image architecture validation', () => {
  let server;
  let serverUrl;
  
  before(() => {
    // Set up test environment
    process.env.MOLA_HOME = testRoot;
    rmSync(testRoot, { recursive: true, force: true });
    mkdirSync(testRoot, { recursive: true });
    
    // Create a simple HTTP server to serve test manifests
    server = createServer((req, res) => {
      const url = new URL(req.url, `http://localhost:${testPort}`);
      
      if (url.pathname === '/manifest-x86_64.json') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({
          version: 'test-image-1.0.0-x86_64',
          architecture: 'x86_64',
          artifacts: [
            {
              name: 'root.ext4',
              url: 'http://example.com/root.ext4',
              bytes: 1024,
              sha256: 'a'.repeat(64)
            }
          ]
        }));
      } else if (url.pathname === '/manifest-aarch64.json') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({
          version: 'test-image-1.0.0-aarch64',
          architecture: 'aarch64',
          artifacts: [
            {
              name: 'root.ext4',
              url: 'http://example.com/root.ext4',
              bytes: 1024,
              sha256: 'b'.repeat(64)
            }
          ]
        }));
      } else if (url.pathname === '/manifest-no-arch.json') {
        // Legacy manifest without architecture field
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({
          version: 'legacy-image-1.0.0',
          artifacts: [
            {
              name: 'root.ext4',
              url: 'http://example.com/root.ext4',
              bytes: 1024,
              sha256: 'c'.repeat(64)
            }
          ]
        }));
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    
    server.listen(testPort, '127.0.0.1');
    serverUrl = `http://127.0.0.1:${testPort}`;
  });
  
  after(() => {
    server?.close();
    rmSync(testRoot, { recursive: true, force: true });
    delete process.env.MOLA_HOME;
  });

  it('rejects aarch64 manifest on x86_64 host', async () => {
    const { downloadImage } = await import(`../src/image.js?nocache=${Date.now()}`);
    
    await assert.rejects(
      async () => {
        await downloadImage({
          url: `${serverUrl}/manifest-aarch64.json`,
          hostArch: 'x86_64',
          log: () => {}
        });
      },
      (error) => {
        assert.strictEqual(error.code, 'ARCH_MISMATCH');
        assert.match(error.message, /host is x86_64/);
        assert.match(error.message, /image.*is aarch64/);
        assert.match(error.message, /Ubuntu image/i);
        return true;
      }
    );
  });

  it('rejects x86_64 manifest on aarch64 host', async () => {
    const { downloadImage } = await import(`../src/image.js?nocache=${Date.now()}`);
    
    await assert.rejects(
      async () => {
        await downloadImage({
          url: `${serverUrl}/manifest-x86_64.json`,
          hostArch: 'aarch64',
          log: () => {}
        });
      },
      (error) => {
        assert.strictEqual(error.code, 'ARCH_MISMATCH');
        assert.match(error.message, /host is aarch64/);
        assert.match(error.message, /image.*is x86_64/);
        return true;
      }
    );
  });

  it('allows manifest without architecture field (backward compatibility)', async () => {
    const { downloadImage } = await import(`../src/image.js?nocache=${Date.now()}`);
    
    // Should not throw - manifest without architecture field is allowed
    // (will fail on artifact download, but that's expected in this test)
    await assert.rejects(
      async () => {
        await downloadImage({
          url: `${serverUrl}/manifest-no-arch.json`,
          hostArch: 'x86_64',
          log: () => {}
        });
      },
      (error) => {
        // Should fail on artifact fetch, not architecture validation
        assert.notStrictEqual(error.code, 'ARCH_MISMATCH');
        return true;
      }
    );
  });

  it('allows download when architectures match', async () => {
    const { downloadImage } = await import(`../src/image.js?nocache=${Date.now()}`);
    
    // Should not throw architecture error (will fail on artifact download)
    await assert.rejects(
      async () => {
        await downloadImage({
          url: `${serverUrl}/manifest-x86_64.json`,
          hostArch: 'x86_64',
          log: () => {}
        });
      },
      (error) => {
        // Should fail on artifact fetch, not architecture validation
        assert.notStrictEqual(error.code, 'ARCH_MISMATCH');
        return true;
      }
    );
  });

  it('allows download when host architecture is not specified', async () => {
    const { downloadImage } = await import(`../src/image.js?nocache=${Date.now()}`);
    
    // Should not throw - when hostArch is null/undefined, skip validation
    await assert.rejects(
      async () => {
        await downloadImage({
          url: `${serverUrl}/manifest-aarch64.json`,
          hostArch: null,
          log: () => {}
        });
      },
      (error) => {
        // Should fail on artifact fetch, not architecture validation
        assert.notStrictEqual(error.code, 'ARCH_MISMATCH');
        return true;
      }
    );
  });
});
