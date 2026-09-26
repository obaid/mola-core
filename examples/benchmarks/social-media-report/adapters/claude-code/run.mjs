import { spawn } from 'node:child_process';
import { mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const prompt = await new Promise(resolve => {
  const chunks = [];
  process.stdin.on('data', chunk => chunks.push(chunk));
  process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString()));
});
const directory = join(tmpdir(), `mola-claude-benchmark-${randomUUID()}`);
const configPath = join(directory, 'mcp.json');
await mkdir(directory, { recursive: true, mode: 0o700 });
try {
  await writeFile(configPath, JSON.stringify({
    mcpServers: {
      mola: {
        command: 'node',
        args: [process.env.MOLA_MCP_ENTRY, 'mcp'],
        env: {
          MOLA_API: process.env.MOLA_API.replace(/\/v1\/?$/, ''),
          MOLA_TOKEN: process.env.MOLA_TOKEN,
        },
      },
    },
  }), { mode: 0o600 });
  const child = spawn('claude', [
    '--print', '--output-format', 'json', '--no-session-persistence',
    '--safe-mode', '--strict-mcp-config', '--mcp-config', configPath,
    '--permission-mode', 'bypassPermissions',
    '--effort', 'low', '--disable-slash-commands',
    '--allowedTools', 'mcp__mola__run_command,mcp__mola__write_file,mcp__mola__read_file',
    ...(process.env.BENCHMARK_MODEL ? ['--model', process.env.BENCHMARK_MODEL] : []),
    prompt,
  ], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const stdout = [], stderr = [];
  child.stdout.on('data', chunk => stdout.push(chunk));
  child.stderr.on('data', chunk => { stderr.push(chunk); process.stderr.write(chunk); });
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); });
  const text = Buffer.concat(stdout).toString();
  process.stdout.write(text);
  let payload = {};
  try { payload = JSON.parse(text); } catch {}
  const usage = payload.usage || {};
  await writeFile(process.env.BENCHMARK_AGENT_METRICS, JSON.stringify({
    agent_version: (await version('claude', ['--version'])).trim(),
    model: process.env.BENCHMARK_MODEL || payload.model || 'default',
    input_tokens: usage.input_tokens,
    output_tokens: usage.output_tokens,
    cache_creation_input_tokens: usage.cache_creation_input_tokens,
    cache_read_input_tokens: usage.cache_read_input_tokens,
    cost_usd: payload.total_cost_usd,
    model_calls: payload.num_turns,
  }, null, 2));
  process.exitCode = code;
} finally {
  await rm(directory, { recursive: true, force: true });
}
function version(command, args) {
  return new Promise(resolve => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'ignore'] });
    const chunks = [];
    child.stdout.on('data', chunk => chunks.push(chunk));
    child.on('close', () => resolve(Buffer.concat(chunks).toString()));
  });
}
