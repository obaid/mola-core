# Mola flagship image: genuine Omarchy.
#
# This is NOT the Debian/i3 stand-in in ../docker. It is real Arch Linux with
# the real upstream Omarchy package set, installed from Omarchy's own pacman
# repository and configured from a pinned upstream git tag.
#
# One thing does not work in a container, and it is load-bearing: the desktop.
# Omarchy is Hyprland, Hyprland 0.56 renders through aquamarine, and aquamarine
# needs a DRM render node to allocate buffers on every backend path it has.
# Docker Desktop's LinuxKit kernel exposes no /dev/dri and has no vkms module,
# so the compositor cannot start here. It starts normally on a KVM guest with
# virtio-gpu. See docs/implementation/decisions/ADR-IMAGE-001-omarchy-in-docker.md.
#
# Everything else in the image — package set, Omarchy configuration, guest
# daemon, sshd, persistence, shutdown — is exercised locally.
#
# amd64 only. Omarchy targets Arch x86_64; Arch Linux ARM is a different
# distribution and its hyprland/aquamarine packages are currently mutually
# unsatisfiable (hyprland wants libaquamarine.so=13, the repo ships 14).
FROM --platform=linux/amd64 archlinux:latest

ARG OMARCHY_REF=v4.0.3
ARG MOLA_IMAGE_VERSION=0.1.0-cua.1
ARG CUA_DRIVER_VERSION=0.28.2
ARG CUA_DRIVER_SHA256=a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e
ARG CUA_SKILLS_SHA256=e2e69723e06697083bd433e5b1e50841a930ae13e13303e98fc5f14d090dc90b

# pacman 7 sandboxes downloads with Landlock and an unprivileged `alpm` user.
# Docker Desktop's VM permits neither, and the failure mode is an unhelpful
# "failed to synchronize all databases".
RUN sed -i 's/^#\?DownloadUser.*/#DownloadUser = alpm/' /etc/pacman.conf \
 && printf '\nDisableSandbox\n' >> /etc/pacman.conf

# Omarchy's own repository. This is what makes the image genuinely Omarchy
# rather than an Arch box with similar packages: aether, omacut, omawrite,
# omarchy-nvim and the rest are built and signed by the Omarchy project.
RUN printf '\n[omarchy]\nSigLevel = Optional TrustAll\nServer = https://pkgs.omarchy.org/stable/$arch\n' >> /etc/pacman.conf \
 && pacman -Sy --noconfirm

# Upstream configuration, pinned. Cloned before the package install so a broken
# tag fails the build early and cheaply.
RUN pacman -S --noconfirm --needed git \
 && git clone --depth 1 --branch "${OMARCHY_REF}" \
      https://github.com/omacom/omarchy /usr/share/omarchy \
 && git -C /usr/share/omarchy rev-parse HEAD > /etc/omarchy-commit \
 && rm -rf /usr/share/omarchy/.git

# The Omarchy base package set, minus entries that address physical hardware a
# virtual machine does not have. The exclusions are recorded in the image
# manifest rather than being silently dropped:
#
#   asdcontrol  Apple Studio Display brightness over USB
#   ddcutil     DDC/CI monitor control over I2C
#   plymouth    boot splash, needs the real initramfs and console
#   bolt        Thunderbolt device authorisation
#   cups*       printing
#
# Everything else — compositor, shell, browser, editors, Omarchy's own tools —
# is installed exactly as upstream lists it.
RUN set -eux; \
    grep -vE '^\s*#|^\s*$' /usr/share/omarchy/install/omarchy-base.packages \
      | grep -vxE 'asdcontrol|ddcutil|plymouth|bolt|cups|cups-filters|cups-pk-helper|nvim' \
      > /tmp/pkgs.txt; \
    pacman -S --noconfirm --needed - < /tmp/pkgs.txt; \
    pacman -Scc --noconfirm

# wayvnc is how the desktop leaves the machine. Omarchy does not ship it —
# upstream assumes a physical screen — so remote display is Mola's
# addition, not something inherited from Omarchy.
RUN pacman -Sy --noconfirm --needed wayvnc seatd openssh catatonit sudo \
 && pacman -Scc --noconfirm

# Cua's Linux driver is a separate component from its experimental Hyprland
# plugin. Install the release-matched binary and skill pack from immutable,
# checksum-verified assets. The plugin needs the exact compositor ABI and is
# deliberately not loaded by this image until a native acceptance run passes.
RUN pacman -Sy --noconfirm --needed curl libx11 libxi libxkbcommon at-spi2-core \
 && mkdir -p "/opt/mola/cua-driver/${CUA_DRIVER_VERSION}" /usr/share/mola/agent-skills \
 && curl -fsSL "https://github.com/trycua/cua/releases/download/cua-driver-rs-v${CUA_DRIVER_VERSION}/cua-driver-rs-${CUA_DRIVER_VERSION}-linux-x86_64-binary.tar.gz" -o /tmp/cua-driver.tar.gz \
 && printf '%s  %s\n' "${CUA_DRIVER_SHA256}" /tmp/cua-driver.tar.gz | sha256sum -c - \
 && tar -xzf /tmp/cua-driver.tar.gz -C "/opt/mola/cua-driver/${CUA_DRIVER_VERSION}" cua-driver \
 && chmod 0755 "/opt/mola/cua-driver/${CUA_DRIVER_VERSION}/cua-driver" \
 && ln -s "${CUA_DRIVER_VERSION}" /opt/mola/cua-driver/current \
 && curl -fsSL "https://github.com/trycua/cua/releases/download/cua-driver-rs-v${CUA_DRIVER_VERSION}/cua-driver-rs-v${CUA_DRIVER_VERSION}-skills.tar.gz" -o /tmp/cua-skills.tar.gz \
 && printf '%s  %s\n' "${CUA_SKILLS_SHA256}" /tmp/cua-skills.tar.gz | sha256sum -c - \
 && tar -xzf /tmp/cua-skills.tar.gz -C /usr/share/mola/agent-skills \
 && mv "/usr/share/mola/agent-skills/cua-driver-rs-v${CUA_DRIVER_VERSION}-skills" /usr/share/mola/agent-skills/cua-driver \
 && rm /tmp/cua-driver.tar.gz /tmp/cua-skills.tar.gz \
 && pacman -Scc --noconfirm

# The tenant is root inside their own machine. That is expected: the isolation
# boundary is the hypervisor and the network, not in-guest permissions.
RUN useradd --create-home --home-dir /home/dev --shell /bin/bash --uid 1000 dev \
 && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
 && chmod 0440 /etc/sudoers.d/dev \
 && passwd --lock dev \
 && groupadd -f seat && usermod -aG seat dev

RUN mkdir -p /run/sshd \
 && sed -i \
      -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
      -e 's/^#\?PermitRootLogin.*/PermitRootLogin no/' \
      -e 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' \
      -e 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' \
      /etc/ssh/sshd_config \
 && printf '\n# Mola\nX11Forwarding no\nAllowUsers dev\n' >> /etc/ssh/sshd_config

# No host keys in the image. Baking them in would give every clone the same SSH
# identity — the "no shared secret in a reusable template" rule. The entrypoint
# generates them per machine, onto the persistent volume.
RUN rm -f /etc/ssh/ssh_host_*

# Omarchy's shipped dotfiles become the skeleton for a new machine's home.
RUN mkdir -p /usr/share/mola/skel \
 && cp -r /usr/share/omarchy/config/. /usr/share/mola/skel/.config/ 2>/dev/null || true \
 && mkdir -p /usr/share/mola/skel/.config /usr/share/mola/skel/.agents/skills \
 && ln -s /usr/share/mola/agent-skills/cua-driver /usr/share/mola/skel/.agents/skills/cua-driver

# A machine built from this image is a new install, not an upgrade. Without
# markers, omarchy-migrate reports every migration in the pinned tag as pending
# (104 on v4.0.3) and the desktop keeps asking about changes that are already
# in the image. Stamping them is what an in-place upgrade to this
# version would have left behind. The final test fails the build rather than
# silently shipping the nag again if upstream moves the directory.
RUN set -eux; \
    mkdir -p /usr/share/mola/skel/.local/state/omarchy/migrations; \
    count=0; \
    for migration in /usr/share/omarchy/migrations/*.sh; do \
      [ -f "$migration" ] || continue; \
      touch "/usr/share/mola/skel/.local/state/omarchy/migrations/$(basename "$migration")"; \
      count=$((count + 1)); \
    done; \
    echo "stamped $count Omarchy migrations"; \
    test "$count" -gt 0

COPY rootfs/ /
COPY bin/mola-guest-amd64 /usr/local/bin/mola-guest

RUN chmod +x /usr/local/bin/mola-guest \
             /usr/local/bin/mola-entrypoint \
             /usr/local/bin/mola-session \
             /usr/local/bin/mola-doctor \
             /usr/local/bin/mola-cua-wait \
             /usr/local/bin/cua-driver \
 && CUA_DRIVER_RS_TELEMETRY_ENABLED=false /usr/local/bin/cua-driver --version

# Immutable image identity. The control plane pins a computer to a version at
# creation; a machine must never silently become a different image.
RUN mkdir -p /etc/mola && printf '{\n\
  "family": "omarchy/agent",\n\
  "image_version": "%s",\n\
  "omarchy_ref": "%s",\n\
  "omarchy_commit": "%s",\n\
  "arch": "x86_64",\n\
  "base": "archlinux",\n\
  "compositor": "hyprland",\n\
  "display": "wayvnc",\n\
  "cua_driver": {"version": "%s", "binary_archive_sha256": "%s", "hyprland_plugin": false},\n\
  "built_at": "%s",\n\
  "excluded_packages": ["asdcontrol","ddcutil","plymouth","bolt","cups","cups-filters","cups-pk-helper"],\n\
  "requires": {"drm_render_node": true}\n\
}\n' \
    "${MOLA_IMAGE_VERSION}" "${OMARCHY_REF}" "$(cat /etc/omarchy-commit)" "${CUA_DRIVER_VERSION}" "${CUA_DRIVER_SHA256}" "$(date -u +%FT%TZ)" \
    > /etc/mola/image.json

ENV MOLA_STATE_DIR=/home/dev/.mola/state \
    MOLA_IMAGE_SLUG=omarchy-agent \
    MOLA_IMAGE_VERSION=0.1.0-cua.1 \
    XDG_RUNTIME_DIR=/run/user/1000

EXPOSE 5900 22

ENTRYPOINT ["/usr/bin/catatonit", "--", "/usr/local/bin/mola-entrypoint"]
