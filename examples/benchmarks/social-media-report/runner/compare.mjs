import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../results/', import.meta.url));
const results = [];
for (const name of (await readdir(root)).filter(name => name.endsWith('.json') && !name.endsWith('.agent.json')).sort()) {
  const result = JSON.parse(await readFile(join(root, name), 'utf8'));
  if (result.schema === 2) results.push(result);
}
const value = input => input === null || input === undefined ? '—' : input;
const pct = input => input === null || input === undefined ? '—' : `${Math.round(input * 100)}%`;
const rows = results.map(result => `| [${result.run_id}](${result.run_id}.md) | ${result.success ? 'pass' : 'fail'} | ${result.agent} | ${result.model} | ${value(result.duration_ms)} | ${value(result.machine_ready_ms)} | ${value(result.time_to_first_action_ms)} | ${value(result.mola?.actions)} | ${value(result.mola?.transport_ms)} | ${pct(result.quality?.supported_claim_ratio)} | ${pct(result.quality?.citation_validity)} |`);
const markdown = `# Social-media report benchmark results

These are reproducible fixture runs, not a universal agent ranking. Compare
quality, end-to-end time, and Mola transport independently.

| Run | Result | Agent | Model | Wall ms | Ready ms | First action ms | Mola actions | Transport ms | Claims | Citations |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
${rows.join('\n') || '| No runs yet | — | — | — | — | — | — | — | — | — | — |'}

Each linked run includes raw JSON, the report produced inside the computer, and
the agent log. Agent action timings come from a run-scoped REST measurement
proxy. Host-global data-plane counters remain separate in each JSON result.
`;
await writeFile(join(root, 'README.md'), markdown);
console.log(join(root, 'README.md'));
