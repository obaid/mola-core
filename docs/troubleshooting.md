# Troubleshooting

## `npx mola-core doctor` says it is not ready

It names what is missing. The usual answers:

**No QEMU with the hvf accelerator.** On a Mac, `brew install qemu`. If you have
a QEMU somewhere else, point at it with `MOLA_QEMU=/path/to/qemu-system-aarch64`.

**No `/dev/kvm`** on Linux. Either virtualisation is off in firmware, or you are
inside a VM whose host does not expose nested virtualisation. Many cloud
instances do not; bare metal always does.

**No python3 or ssh.** Both are used by the automation transport. Install them
through your package manager.

## The engine will not start

**`EADDRINUSE: address already in use 127.0.0.1:4141`** means something is
already listening, usually an engine you forgot about. Find it with
`lsof -nP -iTCP:4141 -sTCP:LISTEN` and stop it, or start this one on another port
with `--port=4242`.

**`Runtime did not become healthy`** means the Python supervisor failed to come
up. Its errors are printed with a `[runtime]` prefix above that line. The most
common cause is a Python that cannot import its dependencies; delete
`~/.mola/python` and start again to rebuild the virtualenv.

## Architecture mismatch during image download

**`Architecture mismatch: host is x86_64, but image ... is aarch64`** (or the
reverse) means the downloaded image was built for a different CPU architecture
than your machine. Images must match the host architecture to run natively under
KVM or HVF.

The default Omarchy image is currently only built for Apple Silicon (aarch64).
On Linux x86_64 hosts, use the Ubuntu desktop image instead:

```sh
MOLA_IMAGE_REF=ubuntu-agent:0.1.0 npx mola-core
```

Or build a compatible image yourself with `bin/native-prepare`. See
[Getting started](getting-started.md#building-your-own-image) for details.

## A machine never reaches ready

Look at its console log:

```sh
tail -40 ~/.mola/runtime/machines/<id>/console.log
```

**`HVF does not support GICv2 emulation`** means the engine chose the wrong
interrupt controller for your QEMU. Stock QEMU on HVF needs GICv3. Set
`MOLA_GIC=3` and create the machine again.

**`QEMU exited during launch`** is reported with the machine's `runtime.log`
beside the console log. That file holds QEMU's own error.

If the machine stays in `booting` with no errors, the guest daemon is not
reporting in. Check that it can reach the engine: the guest dials `10.0.2.2` on
the engine's port, which QEMU maps back to your loopback interface.

## Screenshots come back garbled

Partial or scrambled frames usually mean the capture is racing the compositor
rather than anything being broken. Send an input event first, such as
`{"action": "key", "key": "super"}`, then capture again.

If every frame is wrong rather than the first one, check that the guest's VNC
server is running:

```sh
curl -s -X POST $API/machines/$ID/actions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"action": "exec", "command": "pgrep -a wayvnc; ss -ltn | grep 5900"}'
```

## The desktop URL shows "connecting" forever

The ticket works once and expires after sixty seconds. If you reloaded the page,
opened it twice, or waited too long, ask for a new URL.

Changing only the `#fragment` of a URL does not reload a page, so pasting a fresh
ticket over an old one in the address bar will appear to do nothing. Open it in a
new tab, or reload after changing it.

## Creating or waking fails with a limit

The engine refuses to start more machines than it has room for. Defaults are two
running machines and 8192 MB of reserved memory. Raise them with
`MOLA_MAX_RUNNING` and `MOLA_MAX_MEMORY_MB`, or stop a machine you are
not using.

Stopped machines cost disk, not memory, and do not count against either limit.

## Delete returns 502

The engine could not tear the machine down, so it left it registered rather than
forgetting a virtual machine that may still be running. The response says why.
Retry the delete; if it keeps failing, look for the QEMU process yourself and
check `~/.mola/runtime/machines/<id>/` for what is left behind.

## A machine appears that you did not create

On startup the engine adopts anything the runtime is holding that the registry
does not know about, and names it `adopted-<id prefix>`. That means a previous
engine was killed hard enough that it never wrote the record. The machine is
real and running; delete it normally if you do not want it.

## Everything is slow

Check whether graphics are accelerated:

```sh
npx mola-core doctor
```

`software (llvmpipe)` means the guest renders on the CPU. That works, but a
desktop doing a lot of compositing will feel it. A QEMU built with
virglrenderer reports `accelerated (virgl)` instead.

Also worth checking: how many machines are running, and whether the host has
memory left. Each running machine reserves its full `memory_mb`.

## Machines are filling the disk

Each machine's disk grows as it is used, and a guest image is about a gigabyte.
`npx mola-core uninstall` reports what `~/.mola` occupies before removing it, so
it doubles as a way to see where the space went.

To clear the machines but keep the image:

```sh
npx mola-core uninstall --keep-image
```
