# Cua Driver in Omarchy

Mola's Cua-enabled Omarchy image includes `cua-driver` and its agent skill inside
the computer. It gives agents accessibility, window, browser and desktop tools
alongside Mola's shell, files and screenshot actions. The driver runs in the
guest, not on your laptop or the Mola Core host.

After the computer is ready, open a terminal or SSH into it and check:

```sh
cua-driver --version
cua-driver status
ls ~/.agents/skills/cua-driver
```

The systemd user service starts the driver with the desktop. An agent running
inside the guest can read `~/.agents/skills/cua-driver` for its tool instructions
and connect to the driver's local MCP server. The driver socket stays inside
the guest. Mola Cloud also offers an authenticated Cua session API and the
`mola-cloud` CLI/MCP tools; see the [Mola Cloud Cua guide](https://cloud.mola.sh/docs/cua).

## Image versions and existing disks

Availability depends on the guest image, not just the `mola-core` npm version.
Refreshing a self-hosted engine's image affects new computers; existing disks
keep their own software until you update them. Hosted Mola Cloud upgrades older
retained Omarchy disks after their next ready heartbeat, including when a
stopped computer is woken.

The experimental Hyprland input plugin is disabled in the current image because
its published binary targets a different compositor build. Desktop-scoped
screenshots and input have been verified. Window-scoped foreground/background
input may return MCP `isError`; inspect the result and verify the screen after
actions. Do not assume a successful connection means every advertised tool is
available on this compositor.
