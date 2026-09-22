import { spawn } from 'node:child_process';
import { copyFile, mkdir, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const prompt = await new Promise(resolve => {
  const chunks = [];
  process.stdin.on('data', chunk => chunks.push(chunk));
  process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString()));
});
const home = join(tmpdir(), `mola-codex-benchmark-${randomUUID()}`);
await mkdir(home, { recursive: true, mode: 0o700 });
try {
  await copyFile(join(process.env.CODEX_HOME || join(homedir(), '.codex'), 'auth.json'), join(home, 'auth.json'));
  const cleanApi = process.env.MOLA_API.replace(/\/v1\/?$/, '');
  const tomlString = value => JSON.stringify(value);
  await writeFile(join(home, 'config.toml'), `
[mcp_servers.mola]
command = "node"
args = [${[process.env.MOLA_MCP_ENTRY, 'mcp'].map(tomlString).join(', ')}]
default_tools_approval_mode = "approve"

[mcp_servers.mola.env]
MOLA_API = ${tomlString(cleanApi)}
MOLA_TOKEN = ${tomlString(process.env.MOLA_TOKEN)}
`, { mode: 0o600 });

  const args = [
    'exec', '--json', '--ephemeral', '--skip-git-repo-check',
    '--dangerously-bypass-approvals-and-sandbox',
    '--ignore-rules', '-C', home,
    ...(process.env.BENCHMARK_MODEL ? ['--model', process.env.BENCHMARK_MODEL] : []),
    prompt,
  ];
  const child = spawn('codex', args, {
    env: { ...process.env, CODEX_HOME: home, HOME: home },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const lines = [], stderr = [];
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => { lines.push(chunk); process.stdout.write(chunk); });
  child.stderr.on('data', chunk => { stderr.push(chunk); process.stderr.write(chunk); });
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); });
  const events = lines.join('').split('\n').filter(Boolean).flatMap(line => { try { return [JSON.parse(line)]; } catch { return []; } });
  const usage = events.findLast(event => event.type === 'turn.completed')?.usage || events.findLast(event => event.usage)?.usage || {};
  await writeFile(process.env.BENCHMARK_AGENT_METRICS, JSON.stringify({
    agent_version: (await version('codex', ['--version'])).trim(),
    model: process.env.BENCHMARK_MODEL || 'default',
    input_tokens: usage.input_tokens,
    output_tokens: usage.output_tokens,
    cached_input_tokens: usage.cached_input_tokens,
    model_calls: events.filter(event => event.type === 'turn.completed').length || undefined,
  }, null, 2));
  process.exitCode = code;
} finally {
  await rm(home, { recursive: true, force: true });
}
function version(command, args) {
  return new Promise(resolve => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'ignore'] });
    const chunks = [];
    child.stdout.on('data', chunk => chunks.push(chunk));
    child.on('close', () => resolve(Buffer.concat(chunks).toString()));
  });
}
