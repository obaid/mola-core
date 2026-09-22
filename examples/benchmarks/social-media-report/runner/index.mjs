import { readFile, readdir, writeFile, mkdir } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import process from 'node:process';

const here = new URL('.', import.meta.url);
const argv = process.argv.slice(2);
const opts = {};
for (let index = 0; index < argv.length; index += 1) {
  if (!argv[index].startsWith('--')) continue;
  const next = argv[index + 1];
  opts[argv[index].slice(2)] = !next || next.startsWith('--') ? true : next;
  if (next && !next.startsWith('--')) index += 1;
}
const agent = opts.agent;
const model = opts.model || 'unknown';
const mode = opts.mode || 'fixture';
const command = process.env.BENCHMARK_AGENT_COMMAND;
if (!agent || !command || !process.env.MOLA_TOKEN || !['fixture', 'live'].includes(mode)) {
  throw new Error('Use --agent NAME [--mode fixture|live] and set BENCHMARK_AGENT_COMMAND plus MOLA_TOKEN.');
}
const api = (process.env.MOLA_API || 'http://127.0.0.1:4141/v1').replace(/\/$/, '');
const call = async (path, method = 'GET', body) => {
  const response = await fetch(api + path, {
    method,
    headers: { authorization: `Bearer ${process.env.MOLA_TOKEN}`, 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `Request failed (${response.status}).`);
  return payload.data;
};
const delta = (after, before, key) => Number(after?.[key] || 0) - Number(before?.[key] || 0);
const started = new Date();
const wall = performance.now();
let machine;
try {
  const metricsBefore = await call('/metrics');
  machine = await call('/machines', 'POST', { name: `benchmark-${agent}`, vcpus: 2, memory_mb: 4096, disk_gb: 20 });
  for (;;) {
    const current = await call(`/machines/${machine.id}`);
    if (current.status === 'ready') break;
    if (!['booting', 'starting'].includes(current.status)) throw new Error(`machine became ${current.status}`);
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  let fixtureLine = 'Use public web sources and record every reached or inaccessible URL.';
  if (mode === 'fixture') {
    const fixtureDir = new URL('../fixture/', here);
    for (const name of await readdir(fixtureDir)) {
      const content = await readFile(new URL(name, fixtureDir), 'utf8');
      await call(`/machines/${machine.id}/actions`, 'POST', { action: 'write_file', path: `~/benchmark/fixture/${name}`, content });
    }
    await call(`/machines/${machine.id}/actions`, 'POST', { action: 'exec', command: 'cd ~/benchmark && nohup python3 -m http.server 8765 --directory fixture >/tmp/mola-benchmark-http.log 2>&1 &' });
    fixtureLine = 'Fixture index: http://127.0.0.1:8765/index.html';
  }
  const task = `${await readFile(new URL('../task.md', here), 'utf8')}\n${fixtureLine}\nMola computer id: ${machine.id}\n`;
  const child = spawn('/bin/sh', ['-lc', command], {
    stdio: ['pipe', 'inherit', 'inherit'],
    env: { ...process.env, MOLA_MACHINE: machine.id, BENCHMARK_TASK: task },
  });
  child.stdin.end(task);
  const [code] = await new Promise(resolve => child.on('close', (...values) => resolve(values)));
  if (code !== 0) throw new Error(`agent exited ${code}`);
  const report = await call(`/machines/${machine.id}/actions`, 'POST', { action: 'read_file', path: '~/benchmark/social-report.md' });
  const text = Buffer.from(report.content_base64, 'base64').toString();
  const expected = JSON.parse(await readFile(new URL('../rubric/expected.json', here)));
  const lower = text.toLowerCase();
  const quality = {
    required_sections: expected.required_sections.filter(value => lower.includes(value)).length / expected.required_sections.length,
    citation_validity: expected.sources.filter(value => text.includes(value)).length / expected.sources.length,
    supported_fact_coverage: expected.facts.filter(value => lower.includes(value.toLowerCase())).length / expected.facts.length,
    word_count: text.trim().split(/\s+/).length,
  };
  const metricsAfter = await call('/metrics');
  const result = {
    schema: 1,
    agent,
    agent_version: process.env.BENCHMARK_AGENT_VERSION || 'unknown',
    model,
    mode,
    deterministic: mode === 'fixture',
    mola_commit: process.env.MOLA_COMMIT || 'unknown',
    fixture_version: mode === 'fixture' ? '1' : null,
    started_at: started.toISOString(),
    duration_ms: Math.round(performance.now() - wall),
    success: true,
    machine: { vcpus: 2, memory_mb: 4096, disk_gb: 20 },
    mola_transport: {
      actions: delta(metricsAfter, metricsBefore, 'actions'),
      bytes_in: delta(metricsAfter, metricsBefore, 'bytes_in'),
      bytes_out: delta(metricsAfter, metricsBefore, 'bytes_out'),
      action_latency_ms: metricsAfter.action_latency_ms,
    },
    agent_reported: {
      tokens: process.env.BENCHMARK_AGENT_TOKENS || null,
      cost_usd: process.env.BENCHMARK_AGENT_COST_USD || null,
    },
    quality,
  };
  const base = join(new URL('../results/', here).pathname, `${agent}-${Date.now()}`);
  await mkdir(dirname(base), { recursive: true });
  await writeFile(`${base}.json`, `${JSON.stringify(result, null, 2)}\n`);
  const markdown = `# ${agent} social-report benchmark\n\n- Mode: ${mode}${mode === 'live' ? ' (non-deterministic)' : ' (deterministic fixture)'}\n- Model: ${model}\n- Duration: ${result.duration_ms} ms\n- Required sections: ${(quality.required_sections * 100).toFixed(0)}%\n- Citation validity: ${(quality.citation_validity * 100).toFixed(0)}%\n- Supported facts: ${(quality.supported_fact_coverage * 100).toFixed(0)}%\n- Word count: ${quality.word_count}\n- Mola actions: ${result.mola_transport.actions}\n- Mola bytes: ${result.mola_transport.bytes_in} in / ${result.mola_transport.bytes_out} out\n- Agent tokens: ${result.agent_reported.tokens ?? 'unavailable'}\n- Agent cost: ${result.agent_reported.cost_usd ?? 'unavailable'}\n\nTransport, quality, time, and agent-reported cost are separate measurements.\n`;
  await writeFile(`${base}.md`, markdown);
  console.log(`${base}.json\n${base}.md`);
} finally {
  if (machine && !opts.keep) {
    try { await call(`/machines/${machine.id}`, 'DELETE'); }
    catch { try { await call(`/machines/${machine.id}/stop`, 'POST', {}); } catch {} }
  }
}
