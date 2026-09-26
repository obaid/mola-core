# Cua Driver rollout for hosted Omarchy computers

The factory image includes pinned Cua Driver 0.28.2 and its skill pack. A
retained disk made from an older image receives the same release after its next
ready heartbeat. Stopped computers upgrade when they are next started. The
driver runs inside each guest; the host reaches its MCP server only over that
guest's private SSH connection. The control plane exposes owner-scoped public
API sessions and the `mola-cloud` CLI/MCP tools.

## Release order

1. Build the guest and KVM image with `python3 bin/build-images --guest-only`.
   Verify `/etc/mola/image.json`, `/usr/local/bin/cua-driver --version`, the
   user service, license, and skill link in the built guest. Keep the factory
   image under a new versioned directory; do not replace an existing factory
   image in place.
2. Boot a disposable VM against that staged image on an x86 KVM host. Check
   guest readiness, `cua-driver status`, `GET .../cua`, MCP session creation,
   `list_windows`, `get_desktop_state`, a desktop-scoped click, and session end.
3. Deploy the matching core source and `runtime/` files together. Its heartbeat
   upgrade uses `runtime/cua-install.sh`, and a source/runtime mismatch can
   leave old disks without Cua. Watch for `[cua]` installer errors. Confirm an
   old disposable disk upgrades and remains ready across a stop/start cycle.
4. Activate the staged factory image for new computers using the existing
   atomic image-symlink procedure in `guest-agent-image-update.md`. Keep the
   previous symlink target for rollback. Deploy the control-plane API and the
   `mola-cloud` CLI/MCP release. Verify through an ordinary owner token.

## Expected capability boundary

`available: true` means the driver daemon is reachable. Desktop state,
accessibility inspection, and desktop-scoped pointer input have been verified
on the current Omarchy guest. Cua's experimental Hyprland input plugin is not
installed: its published build kit targets different exact Hyprland/toolchain
packages, so window-scoped foreground and background input can return MCP
`isError`. Inspect every tool result. Do not advertise isolated background
window control until a matching plugin is built and separately accepted.

## Rollback

Revert the factory-image symlink to its recorded target and restart the core
through the normal deployment procedure. Existing disks are independent
clones; restoring a factory-image symlink does not remove Cua from disks
already upgraded. If public Cua access must be disabled, roll back the control
plane and core together. A Cua session is bound to a boot and a core process;
clients should open a new session after either restarts.
