#!/usr/bin/env bash
# Managed, idempotent upgrade for retained Omarchy disks built before Cua was
# included. Runs as dev over the host's existing private SSH connection.
set -euo pipefail

version=0.28.2
release="https://github.com/trycua/cua/releases/download/cua-driver-rs-v${version}"
binary_sha=a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e
skills_sha=e2e69723e06697083bd433e5b1e50841a930ae13e13303e98fc5f14d090dc90b
license_sha=c0779290c1d4783169aa3dbfb55feb505e563ef8a004bbf55298ceffcfbda8d9

if [ "$(id -u)" -ne 1000 ] || [ "$(uname -m)" != x86_64 ]; then
    echo 'Unsupported Cua guest identity or architecture' >&2
    exit 1
fi
if [ -x /usr/local/bin/cua-driver ] \
    && [ "$(/usr/local/bin/cua-driver --version 2>/dev/null || true)" = "cua-driver ${version}" ] \
    && /usr/local/bin/cua-driver status >/dev/null 2>&1; then
    exit 0
fi

# A customer's existing executable is not a managed Mola file. Leave it alone
# and fail visibly instead of silently replacing an agent's own installation.
if [ -e /usr/local/bin/cua-driver ] \
    && ! grep -q '^# Keep the Mola-pinned Cua release' /usr/local/bin/cua-driver 2>/dev/null; then
    echo 'A non-Mola cua-driver exists at /usr/local/bin/cua-driver' >&2
    exit 1
fi

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
curl -fL --retry 3 --connect-timeout 10 --max-time 120 \
    "${release}/cua-driver-rs-${version}-linux-x86_64-binary.tar.gz" -o "$scratch/binary.tar.gz"
curl -fL --retry 3 --connect-timeout 10 --max-time 120 \
    "${release}/cua-driver-rs-v${version}-skills.tar.gz" -o "$scratch/skills.tar.gz"
curl -fL --retry 3 --connect-timeout 10 --max-time 30 \
    'https://raw.githubusercontent.com/trycua/cua/a959b2a23f8099769d0d57e4b8c2c8e70bf7d036/LICENSE.md' \
    -o "$scratch/LICENSE.md"
printf '%s  %s\n' "$binary_sha" "$scratch/binary.tar.gz" \
    "$skills_sha" "$scratch/skills.tar.gz" \
    "$license_sha" "$scratch/LICENSE.md" | sha256sum -c - >/dev/null
tar -xzf "$scratch/binary.tar.gz" -C "$scratch" cua-driver
tar -xzf "$scratch/skills.tar.gz" -C "$scratch"

sudo install -d -m 0755 "/opt/mola/cua-driver/${version}" \
    /usr/share/mola/agent-skills /usr/share/licenses/cua-driver \
    /usr/lib/systemd/user
sudo install -m 0755 "$scratch/cua-driver" "/opt/mola/cua-driver/${version}/cua-driver"
sudo ln -sfn "$version" /opt/mola/cua-driver/current
sudo rm -rf -- /usr/share/mola/agent-skills/cua-driver
sudo cp -a "$scratch/cua-driver-rs-v${version}-skills" /usr/share/mola/agent-skills/cua-driver
sudo install -m 0644 "$scratch/LICENSE.md" /usr/share/licenses/cua-driver/LICENSE.md

# The launcher and service are the same paths used by the new factory image.
sudo install -m 0755 /dev/stdin /usr/local/bin/cua-driver <<'WRAPPER'
#!/usr/bin/env bash
# Keep the Mola-pinned Cua release available to desktop terminals and SSH agents.
set -euo pipefail
export CUA_DRIVER_RS_ENABLE_WAYLAND="${CUA_DRIVER_RS_ENABLE_WAYLAND:-1}"
export CUA_DRIVER_RS_TELEMETRY_ENABLED="${CUA_DRIVER_RS_TELEMETRY_ENABLED:-false}"
export CUA_DRIVER_RS_UPDATE_CHECK="${CUA_DRIVER_RS_UPDATE_CHECK:-false}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    for socket in "${XDG_RUNTIME_DIR}"/wayland-*; do
        case "$socket" in *.lock) continue ;; esac
        if [ -S "$socket" ]; then export WAYLAND_DISPLAY="$(basename "$socket")"; break; fi
    done
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "${XDG_RUNTIME_DIR}/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi
exec /opt/mola/cua-driver/current/cua-driver "$@"
WRAPPER
sudo install -m 0755 /dev/stdin /usr/local/bin/mola-cua-wait <<'WAIT'
#!/usr/bin/env bash
set -euo pipefail
runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
for _ in $(seq 1 120); do
    for socket in "$runtime"/wayland-*; do
        case "$socket" in *.lock) continue ;; esac
        [ -S "$socket" ] && exit 0
    done
    sleep 1
done
exit 1
WAIT
sudo install -m 0644 /dev/stdin /usr/lib/systemd/user/mola-cua-driver.service <<'SERVICE'
[Unit]
Description=Cua Driver for the Mola Omarchy desktop
[Service]
Type=simple
ExecStartPre=/usr/local/bin/mola-cua-wait
ExecStart=/usr/local/bin/cua-driver serve
Restart=on-failure
RestartSec=2
[Install]
WantedBy=default.target
SERVICE
mkdir -p "$HOME/.agents/skills"
if [ ! -e "$HOME/.agents/skills/cua-driver" ] && [ ! -L "$HOME/.agents/skills/cua-driver" ]; then
    ln -s /usr/share/mola/agent-skills/cua-driver "$HOME/.agents/skills/cua-driver"
fi
systemctl --user daemon-reload
systemctl --user enable --now mola-cua-driver.service
for _ in $(seq 1 20); do
    /usr/local/bin/cua-driver status >/dev/null 2>&1 && exit 0
    sleep 1
done
echo 'Cua Driver daemon did not become ready' >&2
exit 1
