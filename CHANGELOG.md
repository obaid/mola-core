# Changelog

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
