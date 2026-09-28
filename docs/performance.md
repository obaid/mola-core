# Action performance

`bin/benchmark-actions` measures the Mola transport around a ready computer. It
records cold and warm latency, p50/p90/p95/p99, response bytes, and the 20-action
mixed loop used by the data-plane issues.

```sh
MOLA_TOKEN=... MOLA_ACTION_TIMING=1 \
  bin/benchmark-actions --machine <id> --transport both \
  --iterations 20 --virtualization kvm --vcpus 2 --memory-mb 4096 \
  --json > benchmark.json
```

`both` runs the complete workload through REST and one persistent action session,
so the output shows connection overhead separately from guest execution. Use
`--transport rest` when collecting a baseline from a release without sessions.
Run the same command from the other checkout to compare revisions. Keep the host,
image, profile, computer snapshot and network constant. JSON includes OS,
architecture, Node and Mola versions. Do not use agent/model time in this
benchmark: it measures only API request through completed computer primitive.

On Apple Silicon, record `hvf` as the accelerator and use the same macOS/QEMU
versions for both revisions. On Linux/KVM, record the kernel, QEMU version and
whether `/dev/kvm` is available. `GET /v1` reports the active accelerator and
machine defaults; copy those values into the benchmark artifact. Enable
`MOLA_ACTION_TIMING=1` only on a private benchmark host to include worker, SSH,
display, capture and serialization spans. The spans contain no command or file
contents.

Suggested engineering targets for a warm local host are under 100 ms p50 for
`exec true` and small files, under 75 ms p50 for input dispatch, under 150 ms
p50 for screenshots, and no per-action interpreter or connection setup in the
mixed loop. Hardware and display stacks vary, so attach the raw JSON to changes
that claim an improvement.
