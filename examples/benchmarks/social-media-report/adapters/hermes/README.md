# Hermes adapter contract

Launch Hermes non-interactively with the benchmark task from stdin. Configure
the Mola MCP server with `node "$MOLA_MCP_ENTRY" mcp`; the runner provides
`MOLA_API`, `MOLA_TOKEN`, and `MOLA_MACHINE`. The adapter must exit nonzero
on failure and leave the report at `~/benchmark/social-report.md` inside that
computer. Set `BENCHMARK_AGENT_COMMAND` to the launch command.

Record version, model, token, call, and cost fields that Hermes exposes in the
JSON file named by `BENCHMARK_AGENT_METRICS`. Omit unavailable fields.
