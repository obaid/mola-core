# Mola client examples

These are runnable clients of Mola's public interfaces. Mola provisions and
exposes computers. It does not choose a model, plan a task, hold agent memory,
or run an agent loop.

| Example | What it demonstrates | Requires |
|---|---|---|
| [REST](rest/) | Create, wait, shell, files, screenshot, stop, cleanup | public REST API |
| [Action session](action-session/) | Correlation, binary I/O, limits, safe reconnect | action sessions |
| [Playwright/CDP](chromium-cdp/) | Launch Chromium and use a generic secure tunnel | tunnels, Playwright example dependency |
| [Jev](jev/) | Optional bounded external decisions with validated side effects | action sessions; Jev key optional |
| [Claude Code](claude-code/) | Agent using Mola MCP | Claude Code and current MCP |
| [Codex](codex/) | Agent using Mola MCP | Codex and current MCP |
| [OpenClaw](openclaw/) | External harness integration | an MCP-capable OpenClaw release |
| [Hermes](hermes/) | External harness integration | an MCP-capable Hermes release |
| [Social-report benchmark](benchmarks/social-media-report/) | Controlled agent comparison with saved evidence | agent CLI plus a disposable Mola host |

Set `MOLA_API` to the public REST base (normally
`http://127.0.0.1:4141/v1`) and `MOLA_TOKEN` to the operator token. Examples
that act on an existing computer also need `MOLA_MACHINE`.

Run the local unit checks with `npm test`. To smoke-test the public REST and
action-session examples against a disposable real computer:

```sh
MOLA_API=http://127.0.0.1:4141/v1 MOLA_TOKEN=... \
  node examples/smoke.mjs
```

The smoke test creates a computer, validates REST and persistent binary I/O,
then deletes it in a `finally` block. Add `--keep` only when you want to
inspect the disk afterwards. No third-party credentials are stored here.
