# Social-media report benchmark

This benchmark asks external agents to research the same sources inside a clean
2-vCPU/4-GB Omarchy computer and save the same report. It measures agent quality,
end-to-end time, machine readiness, and Mola transport separately. It does not
declare a universal winner.

## Reproduce a fixture run

Start from an idle Mola host, then run either built-in adapter:

```sh
export MOLA_API=http://127.0.0.1:4141/v1
export MOLA_TOKEN=...
export MOLA_COMMIT="$(git rev-parse HEAD)"
export MOLA_IMAGE_REF=omarchy-agent:0.1.0

node runner/index.mjs --agent claude-code --model default
node runner/index.mjs --agent codex --model default
node runner/compare.mjs
```

Claude Code and Codex are launched with only the Mola MCP tools they need. Each
run creates a fresh base-image clone, copies fixture v2 into it, starts a local
HTTP server, enforces a 15-minute timeout, retrieves the report, and deletes the
computer. The runner refuses a host with an active data-plane session or tunnel.

Set `BENCHMARK_AGENT_COMMAND` for another adapter. It receives the exact task
on stdin plus `MOLA_MACHINE`, `MOLA_API`, and `MOLA_TOKEN`. It must exit
nonzero on failure and leave `~/benchmark/social-report.md` in the assigned
computer. Optional agent metrics go to `BENCHMARK_AGENT_METRICS`.

## Live web

Live mode is a showcase, never a CI gate:

```sh
node runner/index.mjs --agent codex --mode live \
  --brand "Example Company" --window "the last 30 days"
```

The report must list every reached, inaccessible, login-gated, or failed URL.
Results are marked non-deterministic.

## Results

[Open the committed result index](results/README.md). Every run links to:

- raw schema-v2 JSON;
- the exact report retrieved from the computer;
- agent stdout/stderr;
- a readable per-run summary.

Fixture quality checks require all sections, all known claims, a valid fixture
URL beside every detected claim, five distinct actions, and no unsupported
numeric claims. Latency histograms are recorded per action with p50/p95 bucket
bounds. Core counters are host-global, so reference runs use an otherwise idle
host. Missing agent token or cost data remains `unavailable`.
