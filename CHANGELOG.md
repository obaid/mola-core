# Changelog

## 1.8.0 — 2026-10-08

- Run private host storage commands with durable, generation-bound receipts.
  A long operation returns HTTP 202 and can be reconciled after a lost response
  or a core restart without replacing a committed restore twice.
- Serialize disk writers, persist and verify restore staging before publishing
  it, and hold a supervisor lease through Linux transform subprocesses.
- Replay an unclean ext4 journal only on unpublished restore staging before
  refreshing the managed guest daemon; reject an unsuccessful final check.
- Keep uncertain disks fenced. Transform deadline expiry requires operator
  reconciliation and a supervisor restart; it is not permission to start a guest.
- Add crash-window, retry, checksum, recreation and subprocess-lease tests.
- Correct the declared Node requirement to 22.15+ or 24+: existing image
  decompression uses built-in Zstd, which is unavailable on Node 20.

## 1.7.0 — 2026-10-02

- Add private Decodo browser-proxy bindings to Ubuntu desktops, with a local
  authenticated HTTP/CONNECT bridge and managed Chrome policies.
- Preserve bindings through boot and restore; remove active inherited settings
  from unbound forks and clear credentials from confirmed destroy tombstones.
- Keep normal desktop streaming and non-browser egress unchanged.

## 1.6.0 — 2026-10-02

- Support lightweight Ubuntu 24.04 XFCE desktops alongside the default Omarchy
  image on native x86_64 KVM hosts. Each computer retains its selected image
  across lifecycle, snapshot and restore operations.
- Add image catalogs and per-image resource defaults, including Ubuntu Lite at
  1 vCPU, 2 GiB RAM and 20 GB disk.
- Provide a shared Ubuntu 24.04-4 image recipe with a persistent Chrome Default
  profile, no startup picker, XFCE tiling shortcuts and the JSON `mola-tile` tool.
- Guest images remain separate artifacts. Updating npm does not replace existing
  images or computers; build recipes require the matching repository checkout.

## 1.5.1 — 2026-09-27

- Reconcile the retained local Cua work with the already-published 1.5.0 source.
  This release refreshes package version metadata without runtime behavior changes.
- Retain the current Cua, action-session, snapshot and public documentation updates.

## 1.5.0 — 2026-09-25

- Add pinned Cua Driver 0.28.2 and its agent skill to new Omarchy image builds.
  Hosted older disks receive the driver after their next ready heartbeat.
- Add private, owner-bound, boot-bound Cua MCP sessions for hosted control planes.
  Sessions expose computer-use tools without exposing the guest driver socket.
- Include the Cua installer and public [Cua guide](docs/cua.md) in the npm
  package. Updating npm alone does not replace a self-hosted guest image or
  upgrade existing self-hosted disks.
- Keep the experimental Hyprland input plugin disabled. Desktop-scoped input
  works; window-scoped input may return MCP `isError` on this compositor.

## 1.4.1 — 2026-09-22

- Document persistent action sessions, binary transfers, changed-screen
  subscriptions, and machine-scoped port tunnels on the public documentation
  site and in `llms.txt`.
- Clarify that the 1 MiB action-session limit applies to file reads and incoming
  binary writes. Screenshot and screen-subscription frames report their own
  exact byte length and may be larger.

## 1.4.0 — 2026-09-21

- Keep a bounded pool of automation workers, OpenSSH control connections and
  boot-scoped display connections warm instead of recreating them per action.
- Add single-use, lifecycle-fenced persistent action sessions with ordered
  requests, binary file and screenshot frames, changed-screen subscriptions,
  explicit backpressure, idle limits and latency metrics.
- Add machine-scoped binary port tunnels for guest loopback services such as
  Chromium CDP, with restricted ports, connection, byte and lifetime limits.
- Add REST-versus-session benchmarks, Node and Python action-session examples,
  a runnable Playwright CDP bridge, and external-agent integration guides.

The REST API remains compatible. Hosted control planes can opt into the new
private session and tunnel grants; existing desktop, SSH, snapshot and lifecycle
contracts are unchanged.

## 1.3.0 — 2026-09-15

- Add resumable direct R2 snapshot export and import using short-lived signed
  multipart requests. The compute host verifies complete artifact checksums and
  never receives the control plane's long-lived object-storage credential.
- Upload parts concurrently, persist R2 receipts across retries, and resume
  ranged downloads before atomically publishing a restored snapshot locally.
- Restrict direct-transfer destinations to Cloudflare R2 S3 endpoints and keep
  signed requests out of the durable lifecycle journal.

## 1.2.1 — 2026-09-15

- Increase resumable hosted snapshot chunks from 768 KiB to 8 MiB so large
  off-host archives finish with far fewer host and object-storage requests.
- Raise the private host API body limit only for authenticated snapshot writes;
  all other request limits remain unchanged.

## 1.2.0 — 2026-09-13

Adds an opt-in private host API for a separate control plane, while keeping the
local operator `/v1` API and `mola` CLI available without hosted configuration.

- Persist lifecycle intent and exact retry results; reject stale boot generations
  and retain deletion tombstones. Readiness requires the current boot's fresh
  heartbeat and shell, display and SSH capabilities.
- Bind desktop and private SSH CONNECT tickets to the current boot and generation.
  Stop, restart, restore and fencing revoke access transports. Hosted SSH key
  delivery retains the engine's required key and other authorized keys.
- Add stopped-disk snapshots, checksum-verified restore, resumable bounded snapshot
  transfers and durable cooperative source fencing. Host-loss recovery still
  requires external fencing and a control plane; snapshots are not continuous
  replication or live migration.
- Preserve Linux guest process identity and QMP control sockets across supervisor
  restarts. Restore remains atomic and resets guest enrollment identity.
- Keep the guest daemon alive after rejected cached credentials, registering
  against the current boot seed before resuming heartbeats. Add a staging tool
  and pinned sidecar refresh for older snapshots containing the previous daemon.
- Exclude Python cache bytecode from npm artifacts and ship hosted-operation
  documentation with the package.

### Upgrade requirements

Keep the existing state directory and its machine journal. Never run concurrent
core processes against one state directory. The private API stays disabled
unless `MOLA_HOST_API=1`; it requires a separate host credential and private
network access. Enabling it does not supply tenant isolation, a public gateway,
automatic failover or billing.

Before replacing an older Linux runtime that used temporary QMP sockets, stop
its guests cleanly with that runtime. The new persistent socket location cannot
adopt old temporary sockets automatically. Subsequent restarts use the durable
process/QMP recovery path.

Hosted restore enables guest-agent refresh by default and fails closed without
the operator-pinned static daemon and manifest. Stage the sidecar-bearing image
before enabling restore. This npm release includes guest runtime support and the
staging tool; it does **not** publish or silently replace factory images or
update existing guests. Build the daemon from the matching repository source and
follow [the image update guide](docs/guest-agent-image-update.md). Restore verifies
snapshot bytes first, then replaces only the managed daemon in a temporary disk;
customer files retain their snapshotted contents.
