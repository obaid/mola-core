# Private host disk operations

These endpoints are for the authenticated control plane under `/internal/v1`.
They are disabled unless `MOLA_HOST_API=1` and use the distinct host bearer
credential. They reject browser origins. They are not customer API endpoints.

All disk identities are UUIDs. Requests cannot choose paths or fetch URLs. A
snapshot is a gzip-compressed raw disk, with SHA-256 for both the artifact and
the uncompressed disk. It contains no VM RAM state. Only a confirmed stopped
computer can be snapshotted or restored; unknown state is never sufficient.
Snapshots are private host-local artifacts until the control plane copies them
elsewhere. Their existence alone does not provide host-loss recovery.

For R2 archives, `snapshot-export` and `snapshot-import-direct` accept
short-lived signed requests for one immutable object. The compute host verifies
the complete artifact checksum and transfers bounded multipart passes directly
to or from the R2 S3 endpoint. Part receipts survive retries, while long-lived
object-store credentials remain only on the control plane. The checksummed chunk
routes below remain available for non-S3 archive disks.

## Snapshot and restore

POST `/machines/{id}/snapshot`, `/restore`, or `/snapshot-delete`:

```json
{
  "operation_id": "stable-command-uuid",
  "generation": 2,
  "snapshot_id": "11111111-1111-4111-8111-111111111111"
}
```

Snapshot creation returns HTTP 201 and `{ "data": manifest }`. Other operations
return 200. GET `/machines/{id}/snapshots/{snapshot_id}` reads the same manifest.
The manifest contains `id`, `format` (`mola-raw-gzip-v1`), `architecture`,
`size_bytes`, `sha256`, `artifact_bytes`, `artifact_sha256`, and `created_at`.
Disk replacement validates both checksums and the declared size before an
atomic rename. Hosted restore then refreshes the fixed platform guest-daemon path
from an operator-pinned sidecar before publishing the temporary disk; customer
files retain the snapshot's contents. The response's `snapshot_sha256` identifies
the verified original snapshot, while `guest_agent_refreshed` records that managed
binary update. Failure leaves the current disk in place. Restore rotates the
agent registration credential and reseeds host-specific identity before the
next boot. It does not start the machine.

Lifecycle and disk operations persist intent before runtime work. Repeating
an operation ID with exactly the same payload returns its saved result. Changing
that payload is rejected. An uncertain operation must be retried with the same
ID before a later mutation can proceed. Results refer to that historical
operation; clients should GET the computer for its current state.

## Transfer a snapshot

1. Create a stopped target machine with the same runtime UUID, sufficient disk
   allocation, matching architecture and the exact same installed image/kernel.
2. POST `/machines/{target_id}/snapshot-import` with `operation_id`, `generation`,
   `snapshot_id`, and the complete source `manifest`.
3. POST source `/snapshot-read` with `{ "snapshot_id": "...", "offset": 0 }`.
   The response data contains `offset`, `next_offset`, `data` (base64), `sha256`,
   and `eof`. Chunks are at most 768 KiB.
4. POST target `/snapshot-write` with `snapshot_id`, `offset`, `data`, `sha256`.
   The response `data.offset` is the next expected byte. Retrying the same bytes
   at the same offset succeeds; changed bytes or gaps fail. Chunk requests do
   not need operation IDs and their bulk content is not stored in the operation
   journal. Import replies include an offset for resuming partial uploads.
5. POST target `/snapshot-seal` with `operation_id`, `generation`, `snapshot_id`.
   The checksum and exact artifact length must match before it becomes visible.
6. Restore the sealed snapshot on the target using the regular restore command.

The target is stopped after transfer. The control plane must not start it until
it has valid fencing evidence for the source. Transfer has no arbitrary URL
fetches; the authenticated control plane relays bounded chunks. This relay is
intended as an initial resumable transfer transport, not a high-throughput
object-storage replication service. Snapshot IDs may not be reused with new
content. The control plane should delete snapshot artifacts before destroying
their owning machine metadata.

## Fence a source machine

POST `/machines/{id}/fence` with `operation_id` and `generation`. The core saves a
permanent fence before contacting the runtime. The runtime also persists its
own fence, requests QEMU exit, and verifies stopped state. Only a successful
response `{ "data": { "id": "...", "fenced": true, "status": "stopped" } }`
is stop evidence. A timeout is not evidence, even though the durable flag may
already prohibit a future start. Delayed and replayed start commands fail after
fencing. Restarting either service does not clear the fence. There is no public
unfence command.

This is cooperative fencing of a reachable host. Host loss cannot be handled
by declaring it dead after a timeout: external power fencing or a separately
implemented expiring lease is required before booting a replica elsewhere.
Recovery from a snapshot loses writes made after that snapshot. Live migration,
shared storage and automatic replication are not implemented by this contract.

## Reusing a retired runtime ID on a previous host

A cold migration may eventually return a computer to a host that remembers its
old tombstone. POST `/machines` accepts explicit `recreate: true` for this case.
The old incarnation must have a completed `destroy` receipt with
`delete_disk: true`; the runtime must positively confirm that the old machine
is absent; and the new create needs a fresh operation ID and a strictly higher
generation. A merely fenced computer, a retained disk, an uncertain runtime or
an equal generation cannot be reused. Standard create without this opt-in
continues to reject reserved IDs.

The old operation journal is retained, but every request below the new
incarnation's generation is rejected before replaying old results. New guest
registration credentials are generated and the old permanent fence disappears
only because the old native metadata and disk were deleted. Migration must
advance the computer's generation even if it remains stopped throughout, so
successive migrations back and forth never reuse an incarnation epoch.

## Linux supervisor restarts

Linux QMP sockets live under the persistent runtime `sockets` directory, not
`/tmp`, because systemd `PrivateTmp` may replace that namespace while QEMU keeps
running. A durable marker is written before launch. Once spawned, it records the
Linux boot UUID, PID and `/proc` process start time. On supervisor restart QMP
reconnects to the same guest. If QMP is absent, the matching live process means
`unknown`; only a confirmed process exit, PID reuse or different host boot can
clear the marker and establish stopped state. These process identities are
read-only evidence and are never used to kill a PID. An incomplete launch
receipt remains unknown and requires operator reconciliation.

When upgrading an older runtime whose Linux QMP sockets were under `/tmp`, stop
its guests cleanly using the old supervisor before replacing the service. The
new location does not relocate a live old QEMU socket automatically.

See [guest-agent image updates](guest-agent-image-update.md) before enabling hosted restore. The matching static daemon sidecar and manifest are required so snapshots containing older agents can re-enroll after credential rotation.
