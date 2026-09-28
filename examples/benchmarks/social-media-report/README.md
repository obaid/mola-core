# Social-media report benchmark

This compares external agents on the same clean 2-vCPU/4-GB Mola environment.
Fixture mode is deterministic and is the regression mode; live-web results are
non-deterministic and must record time, reached URLs, inaccessible sources and
agent/model versions.

Set `BENCHMARK_AGENT_COMMAND` to a command that reads the task on stdin and has
Mola API/MCP access, then run:

```sh
BENCHMARK_AGENT_COMMAND="claude -p" node runner/index.mjs --agent claude-code --model <model>
BENCHMARK_AGENT_COMMAND="codex exec -" node runner/index.mjs --agent codex --model <model>
```

The default `--mode fixture` creates a clean computer and uses only the bundled
versioned pages. Use `--mode live` explicitly for a non-deterministic web run.
Set `BENCHMARK_AGENT_VERSION`, `BENCHMARK_AGENT_TOKENS` and
`BENCHMARK_AGENT_COST_USD` when the agent reports them. The runner writes both
JSON and Markdown, and records Mola transport counters separately from agent
tokens, wall time and rubric quality.

OpenClaw, Hermes and Jev-powered harnesses use the same contract: read stdin,
use `MOLA_MACHINE`, and leave `~/benchmark/social-report.md`. Missing agent-side
token/cost metrics are recorded as unavailable, never inferred. Every run emits
machine-readable JSON; combine those files into Markdown without declaring a
universal winner. Compare duration, Mola transport metrics and rubric quality as
separate dimensions. No API keys, cookies or session credentials belong here.
