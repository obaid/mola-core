# Ubuntu XFCE desktop for native KVM hosts. No GPU or streaming encoder required.
FROM --platform=linux/amd64 ubuntu:24.04 AS ubuntu-base
ARG CUA_DRIVER_VERSION=0.28.2
ARG CUA_DRIVER_SHA256=a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    linux-image-generic initramfs-tools systemd-sysv systemd-resolved \
    xfce4 xfce4-terminal tigervnc-standalone-server tigervnc-common dbus-user-session \
    x11-utils x11-xserver-utils xdotool wmctrl xdg-utils imagemagick python3 python3-tk scrot \
    libgl1-mesa-dri mesa-utils libx11-6 libxi6 libxkbcommon0 at-spi2-core \
    fonts-dejavu-core fonts-liberation openssh-server sudo ca-certificates curl \
    git nano less procps iproute2 jq ripgrep e2fsprogs \
    && curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*
# Ubuntu's base reserves uid 1000 for ubuntu. Give the desktop its standard identity.
RUN (id ubuntu >/dev/null 2>&1 && userdel -r ubuntu || true) \
    && useradd -m -u 1000 -s /bin/bash dev && passwd -l dev \
    && printf 'dev ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/dev && chmod 0440 /etc/sudoers.d/dev \
    && mkdir -p /etc/ssh/sshd_config.d /usr/share/mola/skel /etc/mola \
    && printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers dev\n' > /etc/ssh/sshd_config.d/mola.conf \
    && rm -f /etc/ssh/ssh_host_* /etc/machine-id && touch /etc/machine-id
RUN mkdir -p /opt/mola/cua-driver \
    && curl -fsSL "https://github.com/trycua/cua/releases/download/cua-driver-rs-v${CUA_DRIVER_VERSION}/cua-driver-rs-${CUA_DRIVER_VERSION}-linux-x86_64-binary.tar.gz" -o /tmp/cua.tar.gz \
    && printf '%s  /tmp/cua.tar.gz\n' "$CUA_DRIVER_SHA256" | sha256sum -c - \
    && tar -xzf /tmp/cua.tar.gz -C /opt/mola/cua-driver cua-driver && rm /tmp/cua.tar.gz \
    && chmod 0755 /opt/mola/cua-driver/cua-driver
COPY image/omarchy/bin/mola-guest-amd64 /usr/local/bin/mola-guest
# Reuse the existing persistence, identity, SSH and VNC contract.
COPY image/docker/rootfs/usr/local/bin/mola-entrypoint /usr/local/bin/mola-entrypoint
# Kernel preparation is independent of desktop defaults and image version.
RUN printf 'virtio_pci\nvirtio_blk\nvirtio_net\next4\n' >> /etc/initramfs-tools/modules \
    && update-initramfs -u -k all \
    && cp /boot/vmlinuz-* /boot/vmlinuz-linux && cp /boot/initrd.img-* /boot/initramfs-linux.img
COPY image/ubuntu/rootfs/ /
ARG MOLA_IMAGE_VERSION=24.04-3
RUN chmod 0755 /usr/local/bin/mola-* /usr/local/bin/cua-driver \
    && ln -sf mola-chrome /usr/local/bin/google-chrome \
    && ln -sf mola-chrome /usr/local/bin/google-chrome-stable \
    && mkdir -p /usr/share/mola/skel/.config/google-chrome \
    && touch '/usr/share/mola/skel/.config/google-chrome/First Run' \
    && printf 'MOLA_IMAGE_SLUG=ubuntu\nMOLA_IMAGE_VERSION=%s\nDISPLAY=:1\nLIBGL_ALWAYS_SOFTWARE=1\n' "$MOLA_IMAGE_VERSION" > /etc/mola/ubuntu.env \
    && printf '{"image_version":"%s","family":"ubuntu/xfce","architecture":"x86_64"}\n' "$MOLA_IMAGE_VERSION" > /etc/mola/image.json \
    && ln -sf /etc/systemd/system/mola.service /etc/systemd/system/multi-user.target.wants/mola.service \
    && systemctl enable systemd-networkd systemd-resolved \
    && mkdir -p /etc/systemd/network \
    && printf '[Match]\nName=en* eth*\n[Network]\nDHCP=yes\n' > /etc/systemd/network/20-mola.network

# The Mola entrypoint owns sshd and persistent host keys; avoid Ubuntu socket activation racing it.
RUN systemctl disable ssh.service ssh.socket

# All Ubuntu variants inherit the same browser, agent tools and window manager.
# Build this target as mola/ubuntu-base:<version> for derived desktop images.
FROM ubuntu-base AS guest

FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS disk
RUN apt-get update && apt-get install -y --no-install-recommends e2fsprogs zstd && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,from=guest,source=/,target=/rootfs,ro \
    mkdir -p /output && cp /rootfs/boot/vmlinuz-linux /rootfs/boot/initramfs-linux.img /output/ \
    && cp /rootfs/usr/local/bin/mola-guest /output/mola-guest \
    && truncate -s 16G /tmp/root.ext4 && mkfs.ext4 -F -d /rootfs /tmp/root.ext4 \
    && zstd -T2 -3 /tmp/root.ext4 -o /output/root.ext4.zst && rm /tmp/root.ext4

FROM --platform=$BUILDPLATFORM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends zstd && rm -rf /var/lib/apt/lists/*
COPY --from=disk /output/ /opt/mola/
