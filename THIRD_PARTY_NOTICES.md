# Third-party notices

The root MIT license covers Mola's original code. It does not relicense
third-party operating systems, packages, artwork, fonts, or trademarks.

- Omarchy: https://github.com/omacom/omarchy — MIT. Its name and marks remain
  subject to their owners' rights. Mola is independent and not endorsed.
- Arch Linux and Omarchy image packages: each package retains its upstream
  license. Package metadata and installed license files accompany the image.
- Linux kernel, QEMU, Mesa, and the Debian runtime: retain their component
  licenses, including GPL/LGPL obligations. A binary distribution must include
  the applicable notices and make required corresponding source available.
- Laravel and the original React starter kit: MIT, copyright their respective
  contributors. Composer dependencies are recorded in `composer.lock`.
- React, Inertia, Tailwind, and frontend dependencies: license information is
  recorded by the packages in `package-lock.json`.
- noVNC: MPL-2.0. Source: https://github.com/novnc/noVNC.
- vncdotool: MIT. Source: https://github.com/sibson/vncdotool.
- Cua Driver and its agent skill pack: MIT, copyright Cua AI, Inc. The
  versioned release artifacts come from https://github.com/trycua/cua and the
  full license is included in Omarchy images at
  `/usr/share/licenses/cua-driver/LICENSE.md`.
- Archivo and Martian Mono fonts: SIL Open Font License. Built assets self-host
  downloaded fonts; preserve the associated font notices when distributing.

Before publishing VM/container binaries, inventory the final image's packages,
retain their notices, and fulfill corresponding-source obligations. Build
recipes alone do not replace those obligations. This checkout does not publish
or redistribute release binaries automatically.

## Native preview

The Mac importer uses the operator's installed
[Try Omarchy](https://github.com/omacom/try-omarchy) application; the repository
contains no copy of its QEMU binaries or factory disk. The prepared private
image retains the factory manifest, provenance, package lock and Omarchy license.
Try Omarchy's QEMU, graphics libraries and guest packages retain their individual
licenses. Keeping a recipe in this repository does not authorize redistribution
of those binaries without satisfying their licenses.

ARM remote capture builds [WayVNC](https://github.com/any1/wayvnc) (ISC), pinned
to commit `ae53f076a83ad1ecd0d2adcaa063674a632bfe0f`. The recipe verifies the
source archive hash and retains source and license in
`/usr/local/src/hyperwake/wayvnc-ae53f076a83ad1ecd0d2adcaa063674a632bfe0f`.
Opt-in patches use wlr screencopy and disable VNC clipboard synchronization
for compatibility with the factory's pinned compositor. Copy/paste inside the
guest remains available.
The modified file and its `.upstream` copy remain in that source directory.
