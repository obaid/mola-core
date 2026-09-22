# Social-media report benchmark results

These are reproducible fixture runs, not a universal agent ranking. Compare
quality, end-to-end time, and Mola transport independently.

| Run | Result | Agent | Model | Wall ms | Ready ms | First action ms | Mola actions | Transport ms | Claims | Citations |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| [2026-09-22T17-55-15-482-codex](2026-09-22T17-55-15-482-codex.md) | pass | codex | gpt-5.6-luna | 80587 | 27115 | 47322 | 7 | 2082.46 | 100% | 100% |
| [2026-09-22T17-57-59-285-claude-code](2026-09-22T17-57-59-285-claude-code.md) | pass | claude-code | sonnet | 178366 | 26490 | 102508 | 8 | 2230.399 | 100% | 100% |

Each linked run includes raw JSON, the report produced inside the computer, and
the agent log. Agent action timings come from a run-scoped REST measurement
proxy. Host-global data-plane counters remain separate in each JSON result.
