# Low-latency action sessions

A ready computer can mint a single-use session ticket:

```http
POST /v1/machines/{id}/session
Authorization: Bearer $MOLA_TOKEN
```

Connect to `data.session_url` within 60 seconds. The socket sends a `ready`
message with its limits. Requests are JSON and have unique IDs:

```json
{"id":"1","op":"action","action":{"action":"exec","command":"uname -a"}}
```

The server executes requests in arrival order. Request IDs are unique for the
life of the connection; a duplicate is rejected rather than replayed. At most
16 may be queued. Sessions have a 60-second idle timeout and a 30-minute hard
lifetime. Send `{ "id":"heartbeat-1", "op":"ping" }` while an otherwise idle
session is still needed. A stop, restart, restore or delete closes the session
with code 4001. Obtain a new ticket after every boot; never replay a stale
credential or a side-effecting action whose outcome was reported as unknown.

For `read_file` or `screenshot`, set `"binary":true`. The response is JSON
metadata followed by one binary frame. For a binary `write_file`, send JSON with
`binary_bytes`, then one frame of exactly that length. Payloads are capped at 1
MiB and cannot be interleaved. Metadata includes the exact length and SHA-256
digest. A missing or wrong-length frame fails its request while leaving the
session usable.

Subscribe to changed screen frames with:

```json
{"id":"screen-1","op":"screen.subscribe","max_fps":5}
```

The server caps subscriptions at 10 FPS, coalesces slow clients, sends an initial
frame and then only changed frames. `screen.resync` requests a new full frame;
`screen.unsubscribe` stops it. Slow clients have frames coalesced once their
outbound backlog reaches 2 MiB. Frames are transport only: Mola does not OCR or
interpret them.

## Port tunnels

`POST /v1/machines/{id}/tunnels` with `{ "port": 9222 }` returns a one-use
WebSocket URL. Binary WebSocket data is carried to that port through the
machine's verified SSH identity. Targets are always the selected computer's
loopback interface; callers cannot supply a host. Privileged and platform ports
are denied. Tickets are single-use, must be redeemed within 60 seconds, and are
limited to four tunnels per computer, 32 MiB per connection, 60 seconds idle,
and a 30-minute hard lifetime. Lifecycle changes revoke them.

This is deliberately not a general proxy: the runtime chooses the SSH endpoint,
the caller chooses only a permitted loopback port, browser origins are refused,
and the capability remains bound to the computer's current boot and generation.
Core, SSH, VNC, and privileged guest ports cannot be tunneled.
