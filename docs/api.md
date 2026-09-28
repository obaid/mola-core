# API reference

The engine serves `http://127.0.0.1:4141` by default. Change the port with
`MOLA_PORT` or `--port=`.

## Authentication

Every endpoint under `/v1`, and the asset routes, need a bearer token:

```
Authorization: Bearer <token>
```

The token is printed when the engine starts and stored in `~/.mola/token`.
It is generated once and reused. Delete the file to rotate it; the engine writes
a new one on the next start.

Two routes need no token: `GET /health`, and `GET /desktop`, which carries its
own single-use ticket instead.

## Errors

Failures return a JSON body with a `message`, and an HTTP status that means
something:

| Status | When |
|---|---|
| 400 | the request was malformed, or a bound was exceeded |
| 401 | missing or wrong token |
| 404 | no such machine |
| 409 | the machine is not in a state that allows this |
| 502 | the machine could not be torn down, and is still registered |

## Discovery

### `GET /v1`

Returns the endpoint list and what this host can do.

```json
{
  "service": "mola-engine",
  "version": "0.1.0",
  "host": { "platform": "darwin", "arch": "arm64", "accelerator": "hvf" },
  "endpoints": { "...": "..." }
}
```

### `GET /health`

No token needed. Reports whether the engine is up and whether its QEMU runtime
is answering.

## Machines

### `POST /v1/machines`

Creates a computer and starts it. Every field is optional.

```json
{ "name": "research", "vcpus": 4, "memory_mb": 4096, "disk_gb": 40 }
```

| Field | Default | Range |
|---|---|---|
| `name` | generated | up to 64 characters |
| `vcpus` | host default, normally 4 | 1 to 8 |
| `memory_mb` | host default, normally 4096 | 1024 to 16384 |
| `disk_gb` | host default, normally 40 | 16 to 1024 |

Returns 201 and the machine, in `booting`. The disk is a copy-on-write clone of
the base image, which is why creating one takes about a second regardless of
`disk_gb`.

If the create fails partway, the engine tears down whatever it built before
returning the error. If that cleanup also fails, the machine stays registered
and marked for cleanup rather than being forgotten.

### `GET /v1/machines`

```json
{ "data": [ { "id": "...", "name": "research", "status": "ready", "..." } ] }
```

### `GET /v1/machines/{id}`

```json
{
  "data": {
    "id": "849baf6a-a7e7-4648-afd7-932aa2d3c785",
    "name": "research",
    "status": "ready",
    "runtime_status": "running",
    "vcpus": 4,
    "memory_mb": 4096,
    "disk_gb": 40,
    "created_at": "2026-09-10T21:19:48.777Z",
    "capabilities": { "shell": true, "display": true, "sshd": true },
    "last_heartbeat_at": "2026-09-10T21:22:03.118Z",
    "ssh": { "host": "127.0.0.1", "port": 53352, "user": "dev" }
  }
}
```

`status` is the engine's conclusion. `runtime_status` is what QEMU reports.
They are separate on purpose; see [Concepts](concepts.md).

`capabilities` is what the guest claims about itself. It is recorded as a claim,
not treated as fact about the platform.

### `POST /v1/machines/{id}/start`

Starts a stopped machine. Its disk is unchanged.

### `POST /v1/machines/{id}/stop`

Asks the guest to shut down.

```json
{ "force": true }
```

With `force`, the engine cuts power instead. Faster, and the guest gets no
chance to flush anything.

### `DELETE /v1/machines/{id}`

Stops the machine if it is running, waits for the runtime to agree it has
stopped, then destroys it and its disk.

Returns 502 if the teardown fails. The machine stays registered so you can
retry, with the reason in the response.

### `POST /v1/machines/{id}/desktop`

```json
{ "data": { "desktop_url": "http://127.0.0.1:4141/desktop#t=...", "expires_in": 60 } }
```

Returns 409 if the machine is not running.

The ticket is single use and lives sixty seconds. It travels in the URL
fragment, which browsers do not send to servers, so it stays out of access logs
and `Referer` headers. The page strips it from history once it connects.

### `POST /v1/machines/{id}/session`

Returns a single-use WebSocket URL for many ordered actions. The computer must
be ready. The ticket expires after 60 seconds and the live connection is bound
to the current boot. See [Low-latency action sessions](action-session.md) for
the message, binary frame, screen subscription and error contracts.

### `POST /v1/machines/{id}/tunnels`

```json
{ "port": 9222 }
```

Returns a single-use WebSocket tunnel to that port on the selected computer's
loopback interface. Callers cannot provide a host. Privileged and Mola-managed
ports are denied. Operational limits and the threat model are documented with
the [action-session protocol](action-session.md#port-tunnels).

## Actions

All nine go to `POST /v1/machines/{id}/actions`, one per request. The machine
must be running; otherwise you get 409.

`exec`, `read_file` and `write_file` travel over SSH. The rest go over the
guest's VNC socket. Either way the connection details come from the runtime,
never from the request: a caller says *what* to do, never *where*.

### Shell

```json
{ "action": "exec", "command": "ls -la ~", "timeout": 30 }
```

`timeout` is in seconds, 1 to 120.

```json
{
  "data": {
    "exit_code": 0,
    "stdout": "...",
    "stderr": "",
    "timed_out": false,
    "truncated": false
  }
}
```

`timed_out` and `truncated` are separate fields so a slow command and a chatty
one do not look alike.

### Files

```json
{ "action": "read_file",  "path": "~/notes.md" }
{ "action": "write_file", "path": "~/notes.md", "content": "hello\n" }
```

`~` expands in the guest. Parent directories are created on write.

Reads return `content_base64` and `size`. Base64 because a file is bytes rather
than text. Files over 1 MiB are refused; use `exec` to select a smaller portion.

### Screen

```json
{ "action": "screenshot" }
```

Returns `mime_type` and `image_base64`. The screen is 1280x800.

### Input

```json
{ "action": "click",  "x": 640, "y": 400, "button": 1 }
{ "action": "move",   "x": 640, "y": 400 }
{ "action": "scroll", "direction": "down", "amount": 3 }
{ "action": "type",   "text": "hello world" }
{ "action": "key",    "key": "super-return" }
```

`button` is 1 for left, 2 for middle, 3 for right.

`key` takes one key or a combination joined with `-`, such as `enter`, `escape`,
`ctrl-c`, `super-space`. In Omarchy, `super-return` opens a terminal,
`super-space` the launcher, and `super-k` the keybinding list.

## The guest protocol

`/guest/register`, `/guest/heartbeat` and `/guest/shutdown-ack` are for the
daemon inside a machine, not for you. They authenticate with credentials issued
to that machine, not with your operator token.

A registration token is single use. A guest that loses the response recovers by
proving it holds its enrolment key, rather than by repeating the token.

## Environment variables

| | |
|---|---|
| `MOLA_HOME` | state directory, default `~/.mola` |
| `MOLA_PORT` | listen port, default 4141 |
| `MOLA_QEMU` | a specific QEMU binary to use |
| `MOLA_MAX_RUNNING` | how many machines may run at once, default 2 |
| `MOLA_MAX_MEMORY_MB` | total memory machines may reserve, default 8192 |
| `MOLA_DEFAULT_VCPUS` | vCPU count when a create request omits `vcpus`, default 4 |
| `MOLA_DEFAULT_MEMORY_MB` | memory when a create request omits `memory_mb`, default 4096 |
| `MOLA_DEFAULT_DISK_GB` | disk size when a create request omits `disk_gb`, default 40 |
| `MOLA_GPU` | override the QEMU display device |
| `MOLA_DISPLAY` | override the QEMU display backend |
