# Guest tools and qualification

Private host routes use the existing host bearer token. Each computer must be
ready for its current boot. Desktop/browser tools, Cua and shell actions share a
bounded serial lane per computer. Guest requests and secret values travel only
on SSH stdin; subprocess stderr and private requests are not logged.

These routes are also available to locally managed computers under
`/v1/machines/{id}`. Cloud-managed computers continue to require the private API.
Core stdio MCP exposes browser, app, session and portable software tools.

| Private route | Body |
| --- | --- |
| `/internal/v1/machines/{id}/browser` | `{tool,arguments}` |
| `/internal/v1/machines/{id}/apps` | `{tool,arguments}` |
| `/internal/v1/machines/{id}/session-manifest` | `{tool,arguments}` |
| `/internal/v1/machines/{id}/computer-tools` | `{tool,arguments}` |
| `/internal/v1/machines/{id}/network-tools` | `{tool,arguments}` |
| `/internal/v1/machines/{id}/geometry` | `{width,height}` |
| `/internal/v1/machines/{id}/capture` | `{expected_generation,mode,window_id?,browser_tab?,crop?}` |
| `/internal/v1/machines/{id}/vault-inject` | dedicated secret request below |

Success is HTTP 200 `{data:result}`. Missing dependencies and unqualified engines
return 501. Validation, stale references and wrong URL/generation refuse the
operation. A lost SSH response or timeout reports an unknown outcome; side
effects are not automatically replayed. The private transport limit is 45 seconds;
install work acknowledges promptly and continues as a separately fenced job.

## Browser

The full adapter uses Chrome DevTools Protocol inside the guest. Its debugging
listener binds guest loopback and is never exported as a customer tunnel.

| Tool | Arguments | Result |
| --- | --- | --- |
| `browser_prepare` | `{mode:"full"}` | `{prepared,mode,profile,migration}` |
| `browser_capabilities` | `{}` | actual engine/feature and qualification flags |
| `browser_navigate` | `{url,tab_id?}` | `{tab_id,url}` |
| `browser_snapshot` | `{tab_id?}` | `{tab_id,url,title,text,snapshot_id,ready_state,viewport,elements}` |
| `browser_click` | `{tab_id?,snapshot_id,ref}` | `{success:true}` |
| `browser_fill` | `{tab_id?,snapshot_id,ref,value}` | `{success:true}`; password inputs require vault route |
| `browser_key` | `{tab_id?,key}` | `{success:true}`; finite navigation keys, no global modifiers |
| `browser_evaluate` | `{tab_id?,expression}` | `{value}` |
| `browser_screenshot` | `{tab_id?,crop?}` | PNG frame and geometry |
| `browser_tabs` | `{action:"list"\|"new"\|"select"\|"close",url?,tab_id?}` | `{current_tab,tabs:[{id,url,title}]}` |

Preparation inspects existing Chrome processes. Close a non-managed browser
before preparation: Core refuses to copy a live SQLite profile or kill an
existing customer browser implicitly. It copies the closed
`~/.config/google-chrome` profile to `~/.local/share/mola/browser/profile`, keeps
the original, and preserves that managed profile on later launches.
[Chrome 136 requires a non-default data directory for remote debugging](https://developer.chrome.com/blog/remote-debugging-port).
DOM references bind to one snapshot in one document; a new snapshot or navigation
invalidates the old references. Password values are not included in snapshots.

`mode:"lightweight"` runs the same installed Chrome engine with `--headless=new`,
using the managed persistent profile. Switching modes requires explicit
`allow_restart:true` and clean browser shutdown. An isolated Ubuntu guest
preserved its signed-in cookie and SwiftShader WebGL in both modes. In the
same short fixture, headless peak PSS was 457.9 MB versus 486.7 MB with the UI;
idle CPU did not improve. This is a modest memory saving from removing the UI,
not a different browser engine or a promise of lower CPU usage.

## Apps and portable updates

`app_install` requires a caller-generated `operation_id`, `name`, and `kind`:

- `apt` or `pacman`: `packages` array; package names are validated before argv.
- `deb`: HTTPS `url` and mandatory SHA-256.
- `portable`: HTTPS `url`, SHA-256 and `version`; a single executable is staged.
- `flatpak`: qualified installed Flatpak runtime, `package`, remote `flathub`.
- `script`: explicit bounded setup `script`.

Optional `timeout_seconds` is 10–900 (default 600) and `executable` establishes
launch readiness. Result `{operation_id,name,status:"queued"}` is acceptance,
not completion. `app_status {operation_id}` returns progress or terminal
`completed`, `failed`, or `outcome_unknown`, with sanitized error codes only.
One guest installer lease serializes package managers. The same identity and
payload returns the existing receipt; changed payloads refuse. After a crashed
installer, Core reports uncertainty instead of running a setup script twice.
`app_launch {name,args?}` launches a registered ready executable; `app_list {}`
lists registered applications. A successful package-manager exit without an
`executable` does not claim launch readiness.

Portable software supports `software_state {name}`, `software_pin {name,version}`
(`null` clears), `software_stage` (portable install fields),
`software_activate {name,version}` and `software_rollback {name}`. A checksum and
bounded `--version` probe pass before atomically changing `current`; a failed
probe preserves the previous executable. Versions and prior releases remain
available. Pins constrain activation. This does not advertise rollback for
arbitrary distro packages or customer setup scripts. Chrome/Cua version visibility
is available; their managed installer policy remains separately pinned.

## Sessions and display

`session_save {apps?:[registered names]}` persists a versioned manifest of tabs,
current tab, measured window frames and relaunchable apps; `session_state {}`
reads it; `session_restore {}` tolerates missing applications and unavailable
browser/login state, returning explicit errors. Windows and logins cannot be
reconstructed from pixels. Browser cookies remain in the computer's private
profile. A periodic save agent/recovery interval is not yet qualified.

Ubuntu XFCE creation accepts `display:{width,height}`. Width is 640–3840 and
height 480–2160. Identity seed `MOLA_DISPLAY_GEOMETRY` applies it on boot.
The geometry route checks the actual RandR dimensions before persisting the
new setting; later Wake reseeds it. Unsupported display backends refuse.

Resize uses `/resize {operation_id,generation,vcpus,memory_mb,disk_gb}` and the
existing durable receipt polling contract. It requires a stopped computer,
cannot shrink storage and stages disk growth through real ext4 repair/resize
and strict final validation before atomic publication. Cloud supplies capacity
admission and the rate boundary. Prepared receipts reconcile after a crash.
`GET /internal/v1/capabilities` reports actual host limits and disk-growth tools.
Running checkpoints have a separate operator qualification gate,
`MOLA_RUNNING_CHECKPOINT=1`, disabled by default. The checkpoint freezes the
guest ext4 filesystem, pauses QEMU, copies with an independent deadline
watchdog, resumes QEMU and positively proves filesystem thaw before sealing.
`/checkpoint {operation_id,generation,snapshot_id}` requires the current running
boot generation. Pending or uncertain release retains the original receipt
and blocks other storage/lifecycle work. Successful manifests include
`checkpoint_operation_id`, UUID `checkpoint_attempt`, `consistency:"filesystem"`,
`running_resumed:true` and `filesystem_thawed:true`. This pauses execution during
the disk copy; it does not capture application memory or offer incremental disks.

## Vault and restricted views

`vault-inject` accepts `action:"browser_login"`, exact canonical HTTPS `url`,
optional stable `tab_id`, `username`, `password`, optional `totp`, and optional
unique CSS selectors (`username_selector`, `password_selector`,
`totp_selector`, `submit_selector`). TOTP requires its selector. No navigation
occurs; all fields are validated, the exact URL is checked again synchronously
before filling, and submission is optional. Multi-page flows require a separate
authorized injection at the next exact URL. Return is only `{success:true}`.

`action:"captcha_inject"` requires exact HTTPS URL, stable `tab_id`, provider
`value`, and field `g-recaptcha-response` or `cf-turnstile-response`. It only
sets the dedicated response field and dispatches input/change; arbitrary
callback JavaScript is not accepted.

`action:"type_secret"` requires `value` and exact focused `expected_app` WM_CLASS.
Text is delivered through a Linux anonymous memory file descriptor; no clipboard,
temporary disk file, argv or output contains the value. Hosts without this
capability reject the request. Generic application focus/delivery still needs image
qualification. An administrator or an agent with arbitrary evaluation/shell
access can inspect a credential after use; the vault does not claim to prevent
that access or remove secrets from a browser's own persistent state.

Capture modes are `desktop`, `window` (explicit window ID) and `browser`
(explicit tab ID). Crops contain integer `{x,y,width,height}`, relative to the
target viewport, and must remain inside it. Pixels are cropped inside the guest
before base64/transmission. Failure never falls back to a wider view.
Frames return `{mime_type,image_base64,geometry,target_geometry}`. Generation and
boot are checked before capture/input and again before returning pixels.

Cloud window grants additionally bind private `window_identity` metadata:
`{window_id,pid,start_ticks,boot_id,wm_class,instance_nonce}`. Core resolves the owning local
client PID using XRes 1.2 and Linux process start ticks; `_NET_WM_PID` is not
trusted. Missing libXRes or authenticated server PID proof returns 501. Issuance alone mints a private random window-instance nonce property; capture
and input never mint or replace it. Recreated windows lose the property and
fail closed, even within the same process. Scoped capture verifies this identity
and reads only an existing XComposite offscreen pixmap through one X connection
under a short server grab. Missing compositor/redirection is 501; reading the
window framebuffer directly is not a fallback, because obscured pixels are
undefined. Client-area crops exclude the pixmap border. Scoped pointer events target that
window on the same connection; they do not move the global pointer or focus.
Applications may ignore synthetic events. Window keyboard input remains
unsupported. A restarted process, guest reboot, changed WM_CLASS or XID reused by another process
fails closed. The nonce also fences same-process window recreation; this is not isolation
from a malicious app or guest with X server access. Content changes inside the same bound window remain visible.

Internal `viewer_input` accepts
`{expected_generation,scope:{mode,window_id?,window_identity?,browser_tab?,crop?},input:{action,...}}`.
Cloud binds this immutable scope to its viewer grant and blocks input for
watch-only grants. Core directs pointer input to that target. Browser keyboard
input excludes global shortcuts; cropped keyboard input and all window keyboard
input refuse until focus isolation is qualified. Desktop input can use global
keys only for an interactive full-desktop grant.

## Guest HTTP and managed network

`loopback_http {port,path,method?,body?,headers?,timeout_seconds?,payload?}` uses
guest `127.0.0.1` only, no redirects, 1 MiB request/response limits, timeout at most
30 seconds, and excludes managed ports including the active CDP listener.
An omitted body plus structured payload is JSON encoded; response is
`{status_code,status,body,content_type}`. `exec` job payloads are JSON on guest
process stdin, with optional `MOLA_JOB_RUN_ID`; they are never interpolated into
shell commands or passed in argv.

Network configure/status/teardown require `expected_generation`. Configure and
teardown also require `operation_id` and `allow_restart:true`. Configure takes
`browser_proxy:{provider:"decodo",host,port,username,password,country,configuration_id}`;
teardown binds to the exact observed `configuration_id`. Only per-computer
provider credentials belong here. Existing non-managed Chrome must close first;
explicit consent permits only a managed browser restart. TLS unlock refuses.

An fsynced binding records pending intent before modifying the proxy and becomes
configured only after service health. Same identity/payload reconciles;
replacement identity fences teardown. Status reports configuration, boot and
runtime generation, `scope:"browser_and_proxy_aware_apps"`,
`raw_socket_bypass:true`, `tls_unlock:false`, and optional wire counters.
Cloud must observe configured state and matching identity before revoking the
previous provider credential. Failure retains proxy policy; no DIRECT rollback.

Counters contain boot ID, epoch, bytes up/down and connections, persist across
service restart, and use a new epoch on a new guest boot. Updates flush after
64 KiB, one second of activity, or connection close. They are observability;
provider usage is billing authority. Raw sockets bypass Chrome's managed proxy.

## Evidence and remaining gates

The ordinary suite includes private stdin, URL/crop/generation fencing, durable
installer behavior, failed staged updates, resize preservation and recovery,
and proxy counters. `guest-browser-acceptance.py` runs real Chrome on an owned
temporary profile. `native-resize-real.py` uses actual ext4 tools in an isolated
Linux container and preserves a marker through disk growth and replay.
These fixtures do not establish Linux desktop, signed-in migration, lightweight
resource budget, generic app injection, or running-checkpoint parity by themselves.
