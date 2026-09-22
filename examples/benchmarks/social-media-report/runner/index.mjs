import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import http from 'node:http';
import { basename, dirname, join } from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { MolaClient } from '../../../lib/mola-client.mjs';
import { scoreFixtureReport } from './score.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const argv = process.argv.slice(2);
const opts = {};
for (let index = 0; index < argv.length; index += 1) {
  if (!argv[index].startsWith('--')) continue;
  const next = argv[index + 1];
  opts[argv[index].slice(2)] = !next || next.startsWith('--') ? true : next;
  if (next && !next.startsWith('--')) index += 1;
}
const agent = opts.agent;
const mode = opts.mode || 'fixture';
const timeoutMs = Number(opts['timeout-seconds'] || 900) * 1000;
if (!agent || !['fixture', 'live'].includes(mode)) throw new Error('Use --agent NAME [--mode fixture|live] [--timeout-seconds 900].');
if (mode === 'live' && (!opts.brand || !opts.window)) throw new Error('Live mode requires --brand and --window.');

const defaultAdapter = join(root, 'adapters', agent, 'run.mjs');
const command = process.env.BENCHMARK_AGENT_COMMAND || `node ${JSON.stringify(defaultAdapter)}`;
const client = new MolaClient();
const telemetry = [];
const proxy = http.createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  const started = performance.now();
  const headers = { ...request.headers };
  delete headers.host;
  delete headers['content-length'];
  try {
    const upstream = await fetch(new URL(client.api).origin + request.url, {
      method: request.method,
      headers,
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : body,
    });
    const returned = Buffer.from(await upstream.arrayBuffer());
    if (/\/v1\/machines\/[^/]+\/actions$/.test(request.url) && request.method === 'POST') {
      let action = 'unknown';
      try { action = JSON.parse(body).action || action; } catch {}
      telemetry.push({ action, elapsed_ms: Number((performance.now() - started).toFixed(3)), bytes_in: body.length, bytes_out: returned.length, status: upstream.status, at_ms: Math.round(performance.now() - wallStart) });
      if (firstActionAt === null) firstActionAt = performance.now();
    }
    const returnedHeaders = Object.fromEntries(upstream.headers);
    delete returnedHeaders['content-encoding'];
    delete returnedHeaders['content-length'];
    response.writeHead(upstream.status, returnedHeaders);
    response.end(returned);
  } catch (proxyError) {
    response.writeHead(502, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ message: `Benchmark proxy could not reach Mola: ${proxyError.message}` }));
  }
});
await new Promise((resolve, reject) => {
  proxy.once('error', reject);
  proxy.listen(0, '127.0.0.1', resolve);
});
const proxyApi = `http://127.0.0.1:${proxy.address().port}/v1`;
const numberDelta = (after, before, key) => Number(after?.[key] || 0) - Number(before?.[key] || 0);
const histogramDelta = (after = {}, before = {}) => {
  const buckets = {};
  for (const key of new Set([...Object.keys(after.buckets || {}), ...Object.keys(before.buckets || {})])) {
    buckets[key] = Number(after.buckets?.[key] || 0) - Number(before.buckets?.[key] || 0);
  }
  return {
    count: numberDelta(after, before, 'count'),
    sum: numberDelta(after, before, 'sum'),
    min: after.min ?? null,
    max: after.max ?? null,
    buckets,
    overflow: numberDelta(after, before, 'overflow'),
  };
};
const percentile = (histogram, value) => {
  if (!histogram?.count) return null;
  const target = Math.ceil(histogram.count * value);
  let cumulative = 0;
  for (const [limit, count] of Object.entries(histogram.buckets || {}).sort((a, b) => Number(a[0]) - Number(b[0]))) {
    cumulative += count;
    if (cumulative >= target) return Number(limit);
  }
  return histogram.max;
};
const deltaMetrics = (after, before) => {
  const actionLatency = {};
  for (const action of new Set([...Object.keys(after.action_latency_ms || {}), ...Object.keys(before.action_latency_ms || {})])) {
    const histogram = histogramDelta(after.action_latency_ms?.[action], before.action_latency_ms?.[action]);
    if (histogram.count > 0) actionLatency[action] = { ...histogram, p50_upper_bound_ms: percentile(histogram, 0.5), p95_upper_bound_ms: percentile(histogram, 0.95) };
  }
  const all = Object.values(actionLatency);
  const count = all.reduce((sum, item) => sum + item.count, 0);
  const sum = all.reduce((total, item) => total + item.sum, 0);
  return {
    sessions: numberDelta(after, before, 'sessions'),
    actions: numberDelta(after, before, 'actions'),
    bytes_in: numberDelta(after, before, 'bytes_in'),
    bytes_out: numberDelta(after, before, 'bytes_out'),
    stale_rejects: numberDelta(after, before, 'stale_rejects'),
    backpressure_rejects: numberDelta(after, before, 'backpressure_rejects'),
    reconnects: Math.max(0, numberDelta(after, before, 'sessions') - 1),
    transport_ms: Number(sum.toFixed(3)),
    mean_action_ms: count ? Number((sum / count).toFixed(3)) : null,
    action_latency_ms: actionLatency,
  };
};

const startedAt = new Date();
const wallStart = performance.now();
const runId = `${startedAt.toISOString().replace(/[:.]/g, '-').replace('Z', '')}-${agent}`;
const resultBase = join(root, 'results', runId);
const agentLog = `${resultBase}.agent.log`;
const agentMetricsFile = `${resultBase}.agent.json`;
await mkdir(dirname(resultBase), { recursive: true });

let machine;
let report = '';
let result;
let firstActionAt = null;
let poller;
let metricsBefore = {};
let metricsAfter = {};
let error;
try {
  metricsBefore = await client.metrics();
  if (Number(metricsBefore.active || 0) !== 0) throw new Error('Benchmark host is not idle. Wait for active Mola sessions/tunnels to close.');
  const readyStarted = performance.now();
  machine = await client.createMachine({ name: `benchmark-${agent}-${Date.now().toString(36)}`, vcpus: 2, memory_mb: 4096, disk_gb: 20 });
  machine = await client.waitForStatus(machine.id, 'ready', { timeoutMs: 240_000 });
  const readyMs = Math.round(performance.now() - readyStarted);

  let sources = [];
  if (mode === 'fixture') {
    const fixtureDir = join(root, 'fixture');
    for (const name of (await readdir(fixtureDir)).sort()) {
      const content = await readFile(join(fixtureDir, name), 'utf8');
      await client.action(machine.id, { action: 'write_file', path: `~/benchmark/fixture/${name}`, content });
    }
    await client.action(machine.id, { action: 'exec', command: 'cd ~/benchmark && nohup python3 -m http.server 8765 --directory fixture >/tmp/mola-benchmark-http.log 2>&1 &', timeout: 20 });
    sources = ['http://127.0.0.1:8765/index.html', 'http://127.0.0.1:8765/launch.html', 'http://127.0.0.1:8765/support.html', 'http://127.0.0.1:8765/market.html'];
  }
  const versions = await client.action(machine.id, { action: 'exec', command: "printf 'os='; uname -srmo; printf 'browser='; (chromium --version || google-chrome --version || true); printf 'python='; python3 --version", timeout: 30 });
  const taskTemplate = await readFile(join(root, 'task.md'), 'utf8');
  const task = taskTemplate
    .replaceAll('{{MODE}}', mode)
    .replaceAll('{{BRAND}}', opts.brand || 'Acme Robotics')
    .replaceAll('{{WINDOW}}', opts.window || 'the supplied fixture')
    .replaceAll('{{SOURCES}}', sources.map(source => `- ${source}`).join('\n') || '- Use current public sources and record each URL attempted.')
    + `\nThe benchmark computer id is ${machine.id}. Use that computer only. Do not create, start, stop, or delete computers.\n`;

  // Provisioning and fixture setup are environment preparation, not agent work.
  // Take a second baseline so the per-run Mola figures measure only the agent.
  metricsBefore = await client.metrics();
  const output = [];
  const child = spawn('/bin/sh', ['-lc', command], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      MOLA_API: proxyApi,
      MOLA_MACHINE: machine.id,
      BENCHMARK_TASK: task,
      BENCHMARK_AGENT_LOG: agentLog,
      BENCHMARK_AGENT_METRICS: agentMetricsFile,
      MOLA_MCP_ENTRY: fileURLToPath(new URL('../../../../bin/mola.js', import.meta.url)),
    },
  });
  child.stdin.end(task);
  child.stdout.on('data', chunk => { output.push(chunk); process.stdout.write(chunk); });
  child.stderr.on('data', chunk => { output.push(chunk); process.stderr.write(chunk); });
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; child.kill('SIGTERM'); }, timeoutMs);
  const code = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', resolve);
  });
  clearTimeout(timeout);
  await writeFile(agentLog, Buffer.concat(output));
  if (code !== 0) throw new Error(timedOut ? `Agent exceeded the ${Math.round(timeoutMs / 1000)} second timeout.` : `Agent exited with code ${code}.`);
  const read = await client.action(machine.id, { action: 'read_file', path: '~/benchmark/social-report.md' });
  report = Buffer.from(read.content_base64, 'base64').toString('utf8');
  await writeFile(`${resultBase}.report.md`, report);
  metricsAfter = await client.metrics();
  const mola = deltaMetrics(metricsAfter, metricsBefore);
  const measured = summarizeTelemetry(telemetry);
  let agentReported = {};
  try { agentReported = JSON.parse(await readFile(agentMetricsFile, 'utf8')); } catch {}
  const quality = mode === 'fixture'
    ? await scoreFixtureReport(report, JSON.parse(await readFile(join(root, 'rubric', 'expected.json'), 'utf8')))
    : { evaluated: false, reason: 'Live-web quality requires a separate evidence review.' };
  const durationMs = Math.round(performance.now() - wallStart);
  result = {
    schema: 2,
    run_id: runId,
    agent,
    agent_version: agentReported.agent_version || process.env.BENCHMARK_AGENT_VERSION || 'unknown',
    model: opts.model || agentReported.model || 'default',
    mode,
    deterministic: mode === 'fixture',
    mola_commit: process.env.MOLA_COMMIT || 'unknown',
    image_ref: process.env.MOLA_IMAGE_REF || 'host default',
    fixture_version: mode === 'fixture' ? '2' : null,
    started_at: startedAt.toISOString(),
    duration_ms: durationMs,
    time_to_first_action_ms: firstActionAt === null ? null : Math.round(firstActionAt - wallStart),
    machine_ready_ms: readyMs,
    success: true,
    timeout_ms: timeoutMs,
    machine: { vcpus: 2, memory_mb: 4096, disk_gb: 20 },
    environment: versions.stdout.trim().split('\n'),
    mola: {
      ...measured,
      percent_of_wall_time: durationMs ? Number((measured.transport_ms / durationMs * 100).toFixed(2)) : null,
      measurement: 'run-scoped REST proxy',
      core_data_plane: { ...mola, counters_are_host_global: true },
    },
    agent_reported: agentReported,
    report: { file: basename(`${resultBase}.report.md`), bytes: Buffer.byteLength(report), words: report.trim().split(/\s+/).filter(Boolean).length },
    quality,
  };
} catch (caught) {
  error = caught;
  try { metricsAfter = await client.metrics(); } catch {}
  result = {
    schema: 2, run_id: runId, agent, model: opts.model || 'default', mode,
    deterministic: mode === 'fixture', started_at: startedAt.toISOString(),
    duration_ms: Math.round(performance.now() - wallStart), success: false,
    timeout_ms: timeoutMs, error: { name: caught.name, message: caught.message },
    mola: { ...summarizeTelemetry(telemetry), core_data_plane: deltaMetrics(metricsAfter, metricsBefore) },
  };
} finally {
  clearInterval(poller);
  if (machine && !opts.keep) {
    try { await client.deleteMachine(machine.id); }
    catch (deleteError) {
      result.cleanup_error = deleteError.message;
      try { await client.stopMachine(machine.id, { force: true }); } catch {}
    }
  }
}

await new Promise(resolve => proxy.close(resolve));

await writeFile(`${resultBase}.json`, `${JSON.stringify(result, null, 2)}\n`);
const pct = value => value === undefined ? 'unavailable' : `${Math.round(value * 100)}%`;
const markdown = `# ${agent} social-report benchmark\n\n`
  + `- Result: **${result.success ? 'passed' : 'failed'}**\n`
  + `- Mode: ${mode}${mode === 'fixture' ? ' (deterministic fixture)' : ' (non-deterministic live web)'}\n`
  + `- Model: ${result.model}\n`
  + `- Wall time: ${result.duration_ms} ms\n`
  + `- Machine ready: ${result.machine_ready_ms ?? 'unavailable'} ms\n`
  + `- First meaningful action: ${result.time_to_first_action_ms ?? 'unavailable'} ms\n`
  + `- Mola actions: ${result.mola?.actions ?? 0}; transport: ${result.mola?.transport_ms ?? 0} ms (${result.mola?.percent_of_wall_time ?? 'unavailable'}% of wall time)\n`
  + `- Quality — sections: ${pct(result.quality?.required_sections)}, supported claims: ${pct(result.quality?.supported_claim_ratio)}, citation validity: ${pct(result.quality?.citation_validity)}, actionable observations: ${pct(result.quality?.actionable_observations)}\n`
  + `- Agent tokens/cost: ${result.agent_reported?.input_tokens ?? 'unavailable'} input / ${result.agent_reported?.output_tokens ?? 'unavailable'} output; ${result.agent_reported?.cost_usd === undefined ? 'cost unavailable' : `$${result.agent_reported.cost_usd}`}\n`
  + (result.error ? `- Error: ${result.error.message}\n` : '')
  + `\nArtifacts: [JSON](${basename(`${resultBase}.json`)}) · [agent report](${basename(`${resultBase}.report.md`)}) · [agent log](${basename(agentLog)})\n\n`
  + 'Agent action timings come from the run-scoped REST measurement proxy. Host-global core data-plane counters are retained separately in the JSON.\n';
await writeFile(`${resultBase}.md`, markdown);
console.log(`\nResults:\n${resultBase}.json\n${resultBase}.md${report ? `\n${resultBase}.report.md` : ''}`);
if (error) throw error;

function summarizeTelemetry(entries) {
  const sorted = entries.map(entry => entry.elapsed_ms).sort((a, b) => a - b);
  const quantile = value => sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * value) - 1)] : null;
  const byType = {};
  for (const entry of entries) {
    const group = byType[entry.action] ??= [];
    group.push(entry.elapsed_ms);
  }
  return {
    actions: entries.length,
    action_count_by_type: Object.fromEntries(Object.entries(byType).map(([type, values]) => [type, values.length])),
    bytes_in: entries.reduce((sum, entry) => sum + entry.bytes_in, 0),
    bytes_out: entries.reduce((sum, entry) => sum + entry.bytes_out, 0),
    transport_ms: Number(entries.reduce((sum, entry) => sum + entry.elapsed_ms, 0).toFixed(3)),
    p50_action_ms: quantile(0.5),
    p95_action_ms: quantile(0.95),
    failures: entries.filter(entry => entry.status >= 400).length,
  };
}
