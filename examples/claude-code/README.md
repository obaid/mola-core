# Claude Code with Mola

Add the local Mola MCP server:

```sh
claude mcp add --transport stdio mola -- npx -y mola-core mcp
```

Start Mola in another terminal with `npx mola-core`, then ask Claude Code:

> Create one Mola computer named api-review. Clone the repository, run its test
> suite, and leave a report in ~/review.md. Reuse the returned machine id for
> the whole task. Stop the computer when finished; do not delete it.

Claude Code discovers `create_machine`, `run_command`, file, screen, and
lifecycle tools from MCP. Keep the computer id in the task or project notes.
Start a stopped computer to resume its disk. Stop preserves it; delete destroys
it. Mola owns provisioning and computer I/O. Claude Code still owns planning,
memory, model selection, and tool choice.

For a remote engine, launch Claude with `MOLA_API` set to the engine origin
(without `/v1`) and `MOLA_TOKEN` set. For many low-latency actions, an
external harness can use the action-session example. Obtain a fresh session
after every start/stop. Use a tunnel for native protocols such as CDP.
