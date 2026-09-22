# Hermes with Mola

Expose Mola as a stdio MCP server in the Hermes tool configuration:

```json
{
  "mcpServers": {
    "mola": {
      "command": "npx",
      "args": ["-y", "mola-core", "mcp"]
    }
  }
}
```

Use the matching stdio-MCP field names for the Hermes release you run. Start
Mola before the agent. Tell Hermes to create one computer, store its id in the
workflow state, reuse it for every tool call, and stop it after the task. Start
that id to resume later; delete only when its disk is no longer needed.

Set `MOLA_API` (engine origin, without `/v1`) and `MOLA_TOKEN` in the MCP
process for a remote engine. High-frequency harnesses can use the action-session
example. Request a new session after each lifecycle change. Generic tunnels are
the preferred path for protocols such as CDP.
