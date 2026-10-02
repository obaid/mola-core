import { readFileSync } from 'node:fs';
import { isAbsolute } from 'node:path';

// Operator configuration only. Public requests select immutable refs, never paths.
export function installedImages(env = process.env) {
  if (!env.MOLA_IMAGES_FILE) return {};
  const images = JSON.parse(readFileSync(env.MOLA_IMAGES_FILE, 'utf8'));
  if (!images || Array.isArray(images) || typeof images !== 'object') throw new Error('MOLA_IMAGES_FILE must contain an image map.');
  for (const [ref, image] of Object.entries(images)) {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$/.test(ref)
      || !image || typeof image !== 'object' || typeof image.path !== 'string' || !isAbsolute(image.path)
      || !['kernel_args', 'gpu', 'display'].every(key => typeof image[key] === 'string' && image[key].length > 0)
      || !['x86_64', 'aarch64'].includes(image.architecture)) {
      throw new Error(`Invalid installed image configuration: ${ref}`);
    }
    if (image.default_resources && (!['vcpus', 'memory_mb', 'disk_gb'].every(key => Number.isInteger(image.default_resources[key]))
      || image.default_resources.vcpus < 1 || image.default_resources.vcpus > 8
      || image.default_resources.memory_mb < 1024 || image.default_resources.memory_mb > 16384
      || image.default_resources.disk_gb < 16 || image.default_resources.disk_gb > 1024)) {
      throw new Error(`Invalid image default resources: ${ref}`);
    }
  }
  return images;
}
