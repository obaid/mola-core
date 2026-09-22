# Getting started

This walks through installing the engine, creating a computer, driving it, and
cleaning up. It takes about ten minutes, most of which is the first image build.

## 1. Check the machine you are on

```sh
npx mola-core doctor
```

You should see something like:

```
  platform      darwin/arm64
  python3       yes
  ssh           yes
  docker        yes (prepares images; does not run the guest)
  accelerator   hvf
  qemu          /opt/homebrew/bin/qemu-system-aarch64
  graphics      software (llvmpipe)

  Ready.
```

If it is not ready, it says what is missing. The two common answers:

On a Mac, `brew install qemu`. Any QEMU works. It applies the
`com.apple.security.hypervisor` entitlement to itself when it is built, so the
Hypervisor framework needs no Developer ID and no special runtime.

On Linux, install `qemu-system-x86_64` and make sure `/dev/kvm` exists and your
user can open it. Inside a cloud VM that usually means the provider has to
expose nested virtualisation, which many do not.

`graphics: software (llvmpipe)` is normal and fine. The guest gets a DRM render
node from virtio-gpu and Hyprland renders on the CPU. If your QEMU was built
with virglrenderer the line reads `accelerated (virgl)` instead and the desktop
is faster.

## 2. Start the engine

```sh
npx mola-core
```

The first run downloads a guest image, verifies it and puts it in
`~/.mola/image`. It is about 1.5 GB and takes a few minutes on a decent
connection. Every machine you create afterwards is a copy-on-write clone of it,
which is why creating one takes about a second.

It prints an address, a token, and the endpoint list, then stays in the
foreground. Leave it running and open a second terminal.

```sh
export API=http://127.0.0.1:4141/v1
export TOKEN=...        # copy from the banner
```

The token also lives in `~/.mola/token`, so you can do this instead:

```sh
export TOKEN=$(cat ~/.mola/token)
```

The engine binds to loopback only. Nothing outside your machine can reach it.

## 3. Create a computer

```sh
curl -s -X POST $API/machines \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "scratch"}'
```

You get back a machine with `"status": "booting"`. Defaults are 4 vCPU, 4 GB of
memory and a 40 GB disk; pass `vcpus`, `memory_mb` or `disk_gb` to change one
machine. To choose smaller defaults for every machine on a constrained host,
set `MOLA_DEFAULT_VCPUS`, `MOLA_DEFAULT_MEMORY_MB` and optionally
`MOLA_DEFAULT_DISK_GB` before starting Mola. Explicit values in a create request
always win.

Save the id:

```sh
ID=<the id from that response>
```

## 4. Wait for it to be ready

```sh
curl -s $API/machines/$ID -H "Authorization: Bearer $TOKEN"
```

Poll until `status` is `ready`, usually six to ten seconds. `ready` means the
guest daemon has reported in for this boot and its shell works. A machine whose
QEMU process is running but whose guest has not checked in is still `booting`,
because a process that has started is not the same as a computer you can use.

## 5. Run something on it

```sh
curl -s -X POST $API/machines/$ID/actions \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"action": "exec", "command": "grep PRETTY /etc/os-release; uname -m"}'
```

```json
{
  "data": {
    "exit_code": 0,
    "stdout": "PRETTY_NAME=\"Arch Linux ARM\"\naarch64\n",
    "stderr": "",
    "timed_out": false,
    "truncated": false
  }
}
```

Commands run over SSH as the `dev` user, who has passwordless sudo. They are
capped at 120 seconds; for anything longer, start it in the background and poll
for the result yourself.

## 6. Write and read a file

```sh
curl -s -X POST $API/machines/$ID/actions \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"action": "write_file", "path": "~/notes.md", "content": "hello\n"}'

curl -s -X POST $API/machines/$ID/actions \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"action": "read_file", "path": "~/notes.md"}'
```

Reads come back as `content_base64`, because a file is bytes and the trailing
newline is the byte a file API most often loses.

## 7. Look at the screen

```sh
curl -s -X POST $API/machines/$ID/desktop -H "Authorization: Bearer $TOKEN"
```

```json
{ "data": { "desktop_url": "http://127.0.0.1:4141/desktop#t=...", "expires_in": 60 } }
```

Open that URL in a browser. The desktop is live and interactive.

The ticket sits in the URL fragment, which browsers never send to a server, so
it stays out of access logs and `Referer` headers. It works once and expires
after sixty seconds. Ask for a new URL whenever you need one.

You can also drive the screen from the API. This opens a terminal in Omarchy and
runs a command in it:

```sh
curl -s -X POST $API/machines/$ID/actions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"action": "key", "key": "super-return"}'

curl -s -X POST $API/machines/$ID/actions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"action": "type", "text": "fastfetch"}'

curl -s -X POST $API/machines/$ID/actions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"action": "key", "key": "enter"}'
```

Watch the browser tab while those run.

## 8. Stop and delete

```sh
curl -s -X POST $API/machines/$ID/stop -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE $API/machines/$ID -H "Authorization: Bearer $TOKEN"
```

`DELETE` stops the machine first if it is still running, then destroys it and its
disk. If it cannot tear the machine down it returns 502 and leaves the machine
registered, so you can retry. A delete that quietly forgets a running machine
would leave you with a VM nobody can name.

## Building your own image

The downloaded image suits most people. Build your own to change the package set,
or to avoid the download.

```sh
git clone https://github.com/obaid/mola-core
cd mola-core
npm install
python3 bin/native-prepare --output ~/.mola
```

The script checks its own prerequisites first and names anything missing before
it starts work.

That writes `~/.mola/image`, which is where the engine looks. If you pass a
different `--output`, set `MOLA_HOME` to match. The script never overwrites
an existing image, so move the old directory aside to rebuild.

## Removing everything

The engine keeps its state in `~/.mola`, deliberately outside the installed
package, because `npx` clears its own cache and a machine's disk has to outlive
the tool that made it. Nothing else will ever reclaim it, so there is a command
that does:

```sh
npx mola-core uninstall
```

It lists what it found and what that occupies, asks once, then deletes the
machines, their disks, the guest image, the keys and the token. Add
`--keep-image` to keep the image and clear only the machines, or `--yes` to skip
the question.

## Connecting an agent

There is no agent endpoint yet. Today you give your agent the address and token
and let it call the API directly. The endpoint list at `GET /v1` is written to be
read by one:

```sh
curl -s $API -H "Authorization: Bearer $TOKEN"
```

## Next

- [MCP](mcp.md) to drive a machine from Claude Code or another agent you already use
- [API reference](api.md) for every endpoint and action
- [Concepts](concepts.md) for what the states mean and where data lives
- [Troubleshooting](troubleshooting.md) when something does not work
