# Mola

**Instant computers powered by Omarchy that your AI agents can see, control,
and operate.**

A full Linux desktop, driven entirely through an API. Each machine runs Arch
Linux and Hyprland in its own virtual machine, on hardware you already own. One
second to make one. Seven to use it. Delete it and make another.

```sh
npx mola-core
```

The engine starts and prints an address and a token:

```
  API    http://127.0.0.1:4141/v1
  Token  7_NmOSph4PWtcZP5QpVW4rJJ-9VIlIx3
```

Hand those to your agent. It can create a computer, run shell commands, read and
write files, take screenshots, move the mouse and type, open the desktop in a
browser, and delete the whole thing when it is done.

Each computer is a full [Omarchy](https://omarchy.org) desktop: Arch Linux with
Hyprland, running in its own virtual machine. Creating one takes about a second
and it becomes usable in under ten.

## What you need

Node 20 or newer, plus:

| | |
|---|---|
| Apple Silicon Mac | `brew install qemu`, Python 3.10 or newer, an SSH client |
| Linux x86_64 | `qemu-system-x86_64`, a usable `/dev/kvm`, Python 3.10 or newer, SSH |

Check your machine before you start:

```sh
npx mola-core doctor
```

It reports what it found rather than what it assumes, and names anything that is
missing.

Machines are cloned from a base image. The first run downloads one, verifies its
checksums, and puts it in place, so you learn no second command.

Set `MOLA_IMAGE_URL` to use a different image, or build one with
`bin/native-prepare`. The script checks its own prerequisites and names anything
missing. [Getting started](docs/getting-started.md) covers both.

## Your first computer

Save the token from the startup banner, then:

```sh
export TOKEN=...          # from the banner
export API=http://127.0.0.1:4141/v1

ID=$(curl -s -X POST $API/machines \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "first"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["id"])')
```

Wait for it to come up. A new computer starts in `booting` and reaches `ready`
once the guest has reported in:

```sh
curl -s $API/machines/$ID -H "Authorization: Bearer $TOKEN"
```

Then run something on it:

```sh
curl -s -X POST $API/machines/$ID/actions \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"action": "exec", "command": "uname -a; ls ~"}'
```

And look at the screen:

```sh
curl -s -X POST $API/machines/$ID/desktop -H "Authorization: Bearer $TOKEN"
```

That returns a URL. Open it in a browser and you are looking at the live
desktop, mouse and keyboard included.

[Getting started](docs/getting-started.md) walks through the same path in more
detail, including cleaning up afterwards.

## Use it from Claude Code

The engine ships an MCP server, so an agent you already use can drive a machine.
No code, one line of config:

```sh
claude mcp add mola -- npx -y mola-core mcp
```

Then ask for something that needs a computer: *"make me a Linux machine, install
neovim, and show me the desktop."* [Full setup](docs/mcp.md), including Claude
Desktop and Cursor.

## The API

Every request needs `Authorization: Bearer <token>`. `GET /v1` lists the
endpoints and reports what the host can do.

| | |
|---|---|
| `POST /v1/machines` | create a computer and start it |
| `GET /v1/machines` | list them |
| `GET /v1/machines/{id}` | describe one |
| `POST /v1/machines/{id}/start` | start a stopped computer |
| `POST /v1/machines/{id}/stop` | shut it down |
| `POST /v1/machines/{id}/actions` | do something inside it |
| `POST /v1/machines/{id}/session` | open a persistent low-latency action session |
| `POST /v1/machines/{id}/tunnels` | reach one allowed loopback port securely |
| `POST /v1/machines/{id}/desktop` | get a browser URL for the screen |
| `DELETE /v1/machines/{id}` | destroy it and its disk |

Choose the resources for each computer when you create it:

```json
{ "name": "lean", "vcpus": 1, "memory_mb": 2048, "disk_gb": 20 }
```

The supported ranges are 1–8 vCPU, 1024–16384 MB of memory and 16–1024 GB of
disk. A host can also change the defaults for requests that omit these fields
with `MOLA_DEFAULT_VCPUS`, `MOLA_DEFAULT_MEMORY_MB` and
`MOLA_DEFAULT_DISK_GB`. `GET /v1` reports the active values in
`machine_defaults`.

Nine actions run inside a machine: `exec`, `read_file`, `write_file`,
`screenshot`, `click`, `move`, `scroll`, `type`, and `key`.

Full details in the [API reference](docs/api.md). There is also a
[Postman collection](postman/) that exercises the whole thing in sixteen
assertions.

High-frequency clients can keep one ordered action connection open, transfer
screenshots/files as binary frames, subscribe to changed screen frames, and
open machine-scoped port tunnels. See the [action-session protocol](docs/action-session.md)
and the runnable [client examples](examples/README.md).

## Documentation

The full docs are at **[mola.sh](https://mola.sh)**. The same pages live
in this repository:

- [Getting started](docs/getting-started.md), from install to a working computer
- [MCP](docs/mcp.md), using Mola from Claude Code and other agents
- [API reference](docs/api.md), every endpoint and action
- [Concepts](docs/concepts.md), what a machine is and what its states mean
- [Troubleshooting](docs/troubleshooting.md), failures you are likely to hit
- [Development](docs/development.md), working on the engine itself
- [Action sessions](docs/action-session.md), persistent actions, screen frames and tunnels
- [Performance](docs/performance.md), repeatable transport benchmarks

## What is in this repository

| | |
|---|---|
| `src/`, `bin/` | the engine: HTTP API, guest protocol, desktop viewer |
| `runtime/` | the Python supervisor that drives QEMU, and the automation transport |
| `guest/` | the Go daemon that runs inside a machine and reports back |
| `image/` | guest image builds |
| `postman/` | a collection covering the whole API |

The published npm package contains `bin/`, `src/`, `runtime`, `postman`, docs and examples.
The rest are build inputs.

## Where state lives

Everything the engine owns sits in `~/.mola`, which you can move with
`MOLA_HOME`. It is deliberately outside the package, because `npx` installs
into a cache that gets cleared, and a computer's disk has to outlive the tool
that made it.

```
~/.mola/
  token           the operator token
  keys/           the engine's SSH key, generated on first run
  machines.json   what exists
  runtime/        QEMU supervision and per-machine disks
  image/          the guest base image
  python/         a private virtualenv for screen capture
```

## Keeping the guest image current

The image is downloaded once and reused, so a machine you create next month is
built from the image you fetched today. When a newer one is published the
engine says so at startup and stops there: the image is about 1.5 GB, and
starting the engine is not consent to fetch it.

```sh
npx mola-core start --refresh-image
```

It downloads to a staging directory and swaps it in only once the checksum
matches, so an interrupted refresh leaves the image you already had. Machines
that already exist keep their own disks either way.

## Removing it

```sh
npx mola-core uninstall
```

Deletes every machine and its disk, the guest image, the keys and the token,
then reports how much it freed. It shows you the total and asks before doing
anything. `--keep-image` spares the guest image so the next machine does not
re-download it; `--yes` skips the question.

It only removes what Mola created. A virtual machine it did not start, or a
directory it does not own, is left alone.

## Licence

[Functional Source License 1.1](LICENSE.md). Use Mola for anything, at home
or at work, including inside a product you sell. The one thing you may not do is
sell Mola itself, or a service that does what it does.

Every release becomes Apache 2.0 two years after it ships, and that grant is
irrevocable. Until then the source is published and you can read, change, and run
it, but the terms are not open source by the OSI definition.

Omarchy and the bundled dependencies keep their own licences. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Mola is independent and
is not affiliated with or endorsed by Omarchy, DHH, 37signals, Basecamp, or the
Omacom Foundation.

### Private cloud host API (opt-in pilot)

A separate control panel can manage this host without changing the published
local `/v1` workflow. Set `MOLA_HOST_API=1` and configure a **distinct** secret of
at least 32 characters using `MOLA_HOST_TOKEN` or `MOLA_HOST_TOKEN_FILE`. Without
an explicit token, the engine creates a private `$MOLA_HOME/host-api.token` file.
The existing operator token does not authorize the private API. Keep the service
on a private connection (for example an SSH tunnel); do not expose this token or
the engine directly to tenants. The control panel owns tenant authorization,
account quotas and placement. The host enforces runtime limits and machine fencing.

All requests below use `Authorization: Bearer <host-token>` and JSON. Browser
Origin headers are refused. Routes are under `/internal/v1`:

| Request | Result |
| --- | --- |
| `GET /machines` | Managed machines only, `{data:[...]}` |
| `POST /machines` | Create a stopped machine, HTTP 201 `{data:...}` |
| `GET /machines/{id}` | Current runtime observation, `{data:...}` |
| `POST /machines/{id}/start` | Start an existing machine |
| `POST /machines/{id}/shutdown` | Request graceful shutdown |
| `POST /machines/{id}/force-stop` | Request power off |
| `POST /machines/{id}/destroy` | Stop and destroy, explicit `delete_disk` boolean required |
| `POST /machines/{id}/actions` | Existing local action payload and response |
| `POST /machines/{id}/desktop` | Existing single-use, 60-second desktop ticket URL |

Create accepts exactly:

```json
{
  "id": "de5047e3-3dcd-4b48-9c62-dabb218d70e6",
  "name": "development-1",
  "vcpus": 2,
  "memory_mb": 4096,
  "disk_gb": 20,
  "image_ref": "omarchy-agent:0.1.0",
  "operation_id": "create-de5047e3",
  "generation": 1
}
```

`id` is a lowercase UUID selected by the control panel. `image_ref` must match
`MOLA_IMAGE_REF` (default `omarchy-agent:0.1.0`), the operator's label for the image
already installed on this host. This is an allowlist label, not yet a downloaded
or cryptographically verified image manifest. Enrollment credentials and SSH
keys are generated/selected by core, never supplied by the control panel.

Lifecycle requests carry `operation_id` and a positive integer boot `generation`;
destroy also carries `delete_disk`. Create and its first start share generation 1;
shutdown, force-stop and destroy use that boot generation. The next start uses a
higher generation. A new start cannot reuse a generation with an earlier start
or shutdown intent, and commands from older generations are rejected. Use the same operation
ID **and identical payload** when retrying an uncertain request. IDs contain up to
128 letters, digits, hyphens or underscores. The private state journal persists
intent before runtime work and saves the response afterward. Exact completed
retries replay that saved response even across service restarts; poll `GET` for
the current state. Changed payloads, stale new commands, foreign/local machine
IDs, or commands overtaking a pending operation return HTTP 409. An ambiguous
pending operation must be retried and reconciled first. While create/start is
pending, observations report `starting` (or `unknown` if runtime unavailable);
pending stop/destroy reports `unknown`. None reports ready or authoritative
stopped, even if the runtime currently looks stopped, because the accepted
command may still complete. This prevents premature release of account capacity. Never invent a new
machine ID to recover a timeout.

Destroy leaves a private tombstone and operation history so delayed requests
cannot recreate the same ID. Destroy with `delete_disk:false` retains the disk;
attaching retained disks to new machines requires a future recovery interface.
Local `/v1` reads remain available, but local lifecycle, desktop and action
mutations on managed machines are fenced. Only the private API may control them.

Descriptions include `id`, `name`, resource sizes, `image_ref`, `status`
(`running`, `starting`, `stopped`, `unknown`), `disk_id`, `generation`, `ready`,
`boot_id`, `capabilities` and `last_heartbeat_at`. Readiness requires a fresh guest
heartbeat from a new boot after start and all of shell, display and SSH
capabilities. These capabilities remain guest-reported claims, not a security
boundary. Private desktop tickets are bound to the current boot ID and generation, checked
again at WebSocket redemption. Stop/start/destroy revokes outstanding tickets and
closes existing desktop transports, including connections awaiting validation.
Neither registry secrets nor internal SSH/display ports are returned.
Destroy returns `deleted:true`, `status:stopped` and a null disk ID only when the
disk was deleted. Deleted records are excluded from list and GET returns HTTP 404 with
`code:machine_not_found`. Only that explicit code establishes managed-machine
absence; a plain 404 from a disabled API or wrong route is a configuration fault.

The pilot assumes one core process owns a state directory. Keep `machines.json`
and its tombstones when upgrading, and never run two core processes against that
same directory. The journal has no garbage collection yet. No public gateway,
tenant network isolation or billing system is implemented by enabling this API.


Hosted SSH uses single-use private CONNECT tickets bound to the current machine
boot and generation. The control panel authenticates customer SSH keys; core
accepts only an authenticated gateway's Ed25519 key and never exposes its private
guest connection details. Snapshot transfer, restore, permanent source fencing
and retired-ID recreation are documented in [host disk operations](docs/host-storage.md).
Before enabling hosted restore, prepare the required pinned guest-daemon sidecar
using [guest-agent image updates](docs/guest-agent-image-update.md).

See [1.2.0 release notes](CHANGELOG.md) for upgrade requirements, including
cleanly stopping guests managed by an older Linux runtime before its first
upgrade to persistent QMP sockets. The npm update does not update guest images.
