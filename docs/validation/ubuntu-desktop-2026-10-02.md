# Ubuntu desktop validation — 2026-10-02

Implemented in the working copies of mola-core and the Mola Cloud control plane. No production service, account host, provider order or public IP was changed.

## Built image and live guest

The final Ubuntu build/export completed successfully. Booted a disposable guest using the native runner's generated QEMU command with 1 vCPU, 2048 MB RAM and 20 GB disk. Validation ran on an Apple Silicon Mac using **x86 software emulation (TCG)**, not production KVM. The acceleration restriction in the production runner was not relaxed; the temporary test fixture explicitly selected TCG.

Guest: Ubuntu 24.04.5 LTS, Linux 6.8.0-146-generic, XFCE, TigerVNC and Cua Driver 0.28.2. QEMU display was `none`; no GL-capable virtual device or host GPU was used. The guest's shell, VNC, SSH and Cua readiness probes all reported true after startup.

Verified SSH key authentication; an actual XFCE desktop and Chrome page through Mola's VNC screenshot transport; VNC mouse/keyboard/text events reaching Chrome; and Mola's Cua MCP session opening and listing the browser/desktop windows. [Captured desktop](ubuntu-desktop-2026-10-02.png).

A clean OS shutdown exited QEMU with code 0. A subsequent start loaded the same persistent marker and passed strict SSH host-key verification. The Cua daemon also came back after restart. Both disposable guest boots were shut down cleanly and their VM disks and temporary credentials were removed after verification.

A sample after Chrome loaded reported 592 MB used and 1374 MB available in the 1966 MB guest. This is a one-page workload observation, **not** a memory guarantee, throughput benchmark or measured host-density claim. Boot/readiness timing under TCG is unsuitable for production estimates. Real KVM, concurrent mixed-image load, U.S. egress geography and two-physical-host transfer acceptance remain deployment checks.

The boot test caught Ubuntu's default SSH socket racing the managed sshd. The final image disables that socket/service; the Mola entrypoint owns sshd and persistent host keys.

## Automated verification

- Core Node suite: 111 passed. Subsequent focused API/catalog suite: 33 passed.
- Cloud CLI/MCP suite: 30 passed; Node syntax checks passed.
- New/related control-plane API and native-driver suite: 28 passed.
- Full control-plane suite: 651 passed, five failed, one skipped (657 total).
- PHPStan and Pint checks passed; both repositories' whitespace checks passed.
- Native clone/retry/image selection and snapshot/restore/transfer fixtures also passed inside an amd64 Ubuntu Linux container, exercising GNU sparse/reflink copy and Linux storage behavior.

The five control-plane failures are SpendCapTest (three cases) and UsageLedgerTest (two cases). All five reproduced on a separate **unchanged HEAD archive** using the same date and in-memory test database. Their fixtures span more than the elapsed hours in the current month and their expectations disagree with month-window accounting. Billing code and those test fixtures were not modified for this feature.
