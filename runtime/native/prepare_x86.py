"""Export Mola's Docker-built x86 guest for native Linux/Windows QEMU."""
import json
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid


def prepare(root, output, qemu):
    if platform.system() not in ('Windows', 'Linux') or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('The native x86 preview supports Windows/Linux x86_64 hosts only.')
    if not qemu or not qemu.is_file():
        raise SystemExit('Supply --qemu with the GPU-capable qemu-system-x86_64 executable. See docs/platforms.md.')
    output = output.resolve()
    image = output / 'image'
    if image.exists(): raise SystemExit('Image exists. Use a new --output; existing disks are never overwritten.')
    subprocess.run([sys.executable, str(root / 'bin/build-images'), '--guest-only'], check=True)
    image.mkdir(parents=True, mode=0o700)
    name = 'mola-export-' + uuid.uuid4().hex[:12]
    subprocess.run(['docker', 'create', '--name', name, 'mola/omarchy-kvm:local'], check=True, stdout=subprocess.DEVNULL)
    try:
        subprocess.run(['docker', 'cp', name + ':/opt/mola/.', str(image)], check=True)
    finally:
        subprocess.run(['docker', 'rm', '-v', name], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'zstd', '-v', str(image) + ':/output',
                    'mola/omarchy-kvm:local', '-d', '--sparse', '/output/root.ext4.zst', '-o', '/output/root.ext4'], check=True)
    (image / 'root.ext4.zst').unlink()
    # Hosted restores replace the managed guest daemon in an old snapshot from
    # this trusted sidecar. A newly built image must carry it too.
    guest = image / 'mola-guest'
    shutil.copy2(root / 'image/omarchy/bin/mola-guest-amd64', guest)
    guest.chmod(0o755)
    with guest.open('rb') as stream:
        guest_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    image_manifest = json.loads(subprocess.check_output([
        'docker', 'run', '--rm', '--entrypoint', 'cat', 'mola/omarchy:dev',
        '/etc/mola/image.json',
    ]))
    (image / 'guest-agent.json').write_text(json.dumps({
        'schema': 1, 'file': 'mola-guest', 'architecture': 'x86_64',
        'version': image_manifest['image_version'], 'sha256': guest_sha,
    }, indent=2) + '\n')
    windows = platform.system() == 'Windows'
    config = {'schema': 1, 'architecture': 'x86_64', 'qemu': str(qemu.resolve()), 'image': str(image),
              'kernel_args': 'root=/dev/vda rw rootwait console=hvc0 systemd.unit=multi-user.target',
              'display': 'sdl,gl=on' if windows else 'egl-headless', 'gpu': 'virtio-vga-gl',
              'cpu': 'host', 'connect_host': 'host.docker.internal', 'guest_endpoint': 'http://10.0.2.2:8080',
              'max_running': 2, 'max_memory_mb': 8192}
    (output / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    print('Native x86 preview prepared. Hardware validation is required: ' + str(output / 'config.json'))
