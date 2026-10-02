# Ubuntu desktops on mixed-image KVM hosts

Ubuntu 24.04 LTS + XFCE + TigerVNC is an additional desktop image. It uses CPU rendering (`LIBGL_ALWAYS_SOFTWARE=1`) and a headless X server. It requires no host GPU, virtual GL context, Sunshine or Moonlight encoder. This is the same Ubuntu OS family investigated on Boat, with a lighter XFCE/VNC desktop stack; it is not a copy of Boat's proprietary image or streaming service.

The hosted create contract remains `/internal/v1/machines`. `image_ref` is an immutable operator-installed reference. The default Omarchy directory and reference remain supported. Each native machine now persists its selected image; clone, kernel/initramfs, display options and trusted agent refresh all use that image. New snapshot manifests carry the image reference and incompatible imports fail before touching the disk. Legacy manifests without a reference belong to the host's legacy default image. Keep that default stable for legacy machines.

## Build and install

Build on a Docker machine (including an ARM development laptop):

```sh
python3 bin/prepare-ubuntu --output /srv/mola/images/ubuntu-24.04-1
```

This requires Docker and exports a 16 GiB sparse ext4 base, kernel, initramfs and checksum-verified guest-agent sidecar. The output must be a new directory. Failed/incomplete staging directories are rejected by the runner. The compiled guest agent uses the existing build pipeline. Ubuntu packages and Chrome track security updates at build time: pin each accepted build under a new image ref and retain its artifacts; never rebuild over a ref used by existing machines.

On the **Linux x86_64 KVM host**, keep its existing Omarchy installation. Write a private, operator-owned JSON catalog:

```json
{
  "ubuntu-xfce:24.04-1": {
    "path": "/srv/mola/images/ubuntu-24.04-1",
    "architecture": "x86_64",
    "kernel_args": "root=/dev/vda rw rootwait console=hvc0 systemd.unit=multi-user.target",
    "gpu": "virtio-gpu-pci",
    "display": "none",
    "default_resources": { "vcpus": 1, "memory_mb": 2048, "disk_gb": 20 }
  }
}
```

Add `MOLA_IMAGES_FILE=/etc/mola/images.json` to the existing mola-core service environment. Keep `MOLA_IMAGE_REF` at the existing Omarchy ref. Catalog paths and QEMU arguments come only from operator configuration, never customer request fields. All installed images must match the host architecture. This Ubuntu image is x86_64; it cannot coexist with the ARM Omarchy image on an Apple Silicon host using hardware acceleration.

Set `MOLA_MAX_MEMORY_MB` and `MOLA_MAX_RUNNING` from the host's **usable guest budget**, leaving RAM for the host and QEMU overhead. The defaults of 8192 MB and two running VMs deliberately remain unchanged. Do not raise these without updating the panel host capacity to the same usable budget. RAM admission is a hard reservation; CPU oversubscription is an explicit operator setting, not extra guaranteed CPU performance.

Restart the core service after installing the complete catalog. Authenticate `GET /internal/v1/images` with the private host token to discover references (no host paths are exposed). Then perform the acceptance checks below before admitting the image in the panel. Existing machines continue to use their pinned image across stop/start and runtime restarts.

For the standalone Python `bin/native-host` used by the panel’s `native` driver, add the same map under `images` in its operator `config.json`; `MOLA_IMAGES_FILE` is loaded by the Node mola-core service, not that standalone launcher.

The local operator `POST /v1/machines` also accepts `image_ref` and uses its catalog `default_resources` when sizes are omitted. The cloud host endpoint requires explicit resource sizes supplied by the control plane.

## Acceptance before admitting a host

On a real Linux/KVM host, create a disposable 1 vCPU / 2048 MB / 20 GB Ubuntu VM. Check guest registration and readiness, desktop screenshot, keyboard/mouse, Cua status and calls, Chrome browsing, file transfer and SSH. Write a persistent marker, stop/start and verify it. Repeat snapshot/restore and cold migration to a second host with the **same** immutable image installed. Delete only these test machines and their snapshots afterwards.

Run both Omarchy and Ubuntu concurrently and sample guest/host RAM and CPU with a representative browser workload. A browser with many tabs or an agent build may require Small or Standard. Hardware density and performance are not established by mocked host tests or cross-architecture TCG emulation.

The native runner's QEMU user-mode networking sends traffic through the host's outbound route and public IP. Guest VNC/SSH forwards bind host loopback and are accessed through the existing authenticated gateways. This does not allocate one public IPv4 per desktop. A U.S. egress pool requires verified U.S. outbound IPs; changing a metadata label cannot change an IP's geography.

## Implementation validation

See [the validation record](validation/ubuntu-desktop-2026-10-02.md) for the real guest checks, automated results and limits of the local emulation test.
