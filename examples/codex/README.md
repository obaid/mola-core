# Codex with Mola

Add the local Mola MCP server:

```sh
codex mcp add mola -- npx -y mola-core mcp
```

Start Mola in another terminal with `npx mola-core`, then ask Codex:

> Create one Mola computer named signup-test. Start the app, test its signup
> flow, and save findings in ~/signup-report.md. Reuse the returned machine id.
> Stop the computer when finished; do not delete it.

Codex discovers lifecycle, shell, file, screen, and desktop tools through MCP.
Keep the computer id in the task or repository notes. A stopped computer keeps
its disk; deletion is permanent. Mola does not provide the agent loop or model.

For a remote engine, launch Codex with `MOLA_API` set to the engine origin
(without `/v1`) and `MOLA_TOKEN` set. Use the action-session example when a
custom harness needs repeated low-latency I/O, requesting a new session after a
lifecycle change. Use the generic tunnel for CDP and similar native protocols.
