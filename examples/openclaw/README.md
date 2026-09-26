# OpenClaw with Mola

Run Mola's stdio MCP server as an external OpenClaw tool process:

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

Use the equivalent stdio-MCP block for the OpenClaw release you run. Start Mola
first. Instruct the agent to create one computer, retain the returned id in its
workspace state, prefer shell/file tools, and stop the computer when idle.
Deletion removes the retained disk.

A remote engine uses `MOLA_API` (engine origin, without `/v1`) and
`MOLA_TOKEN` in the MCP process environment. A custom OpenClaw harness can use
the persistent action-session client for repeated actions and the tunnel client
for native protocols. Sessions are boot-bound: reconnect with a newly minted
session after every stop/start and never blindly replay an uncertain action.
