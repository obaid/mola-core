# Mola client examples

Mola provisions and exposes computers. The agent, model, planner, memory and
orchestration remain outside Mola.

| Example | What it demonstrates | Requires |
|---|---|---|
| `rest/` | Basic computer primitives | current REST API |
| `action-session/` | Many ordered actions over one connection | action sessions |
| `chromium-cdp/` | Playwright through a generic secure port tunnel | tunnels |
| `jev/` | Optional external bounded decisions | action sessions recommended |
| `claude-code/` | Claude Code using Mola MCP | current MCP |
| `codex/` | Codex using Mola CLI/MCP | current CLI/MCP |
| `openclaw/` | External harness integration | current MCP/API |
| `hermes/` | External harness integration | current MCP/API |

Examples read `MOLA_API`, `MOLA_TOKEN` and `MOLA_MACHINE`; they never contain
credentials. Stop a computer when work may resume and delete it only when its
disk is no longer needed.
