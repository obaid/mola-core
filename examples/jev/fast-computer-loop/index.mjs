import { MolaClient } from '../../lib/mola-client.mjs';

const client = new MolaClient();
const machine = process.env.MOLA_MACHINE;
if (!machine) throw new Error('Set MOLA_MACHINE to a ready machine id.');
const maxSteps = Number(process.env.JEV_MAX_STEPS || 4);
const candidates = [
  { id: 'launch', description: 'Fetch the product launch discussion.', url: 'http://127.0.0.1:8765/launch.html' },
  { id: 'support', description: 'Fetch customer praise and complaints.', url: 'http://127.0.0.1:8765/support.html' },
  { id: 'market', description: 'Fetch competitor and market mentions.', url: 'http://127.0.0.1:8765/market.html' },
  { id: 'done', description: 'Stop when every research source has been fetched.' },
];
const visited = new Set();
const session = await client.actionSession(machine);
try {
  for (let step = 0; step < maxSteps; step += 1) {
    const available = candidates.filter(candidate => candidate.id === 'done' || !visited.has(candidate.id));
    const choice = await choose(available, visited);
    if (!available.some(candidate => candidate.id === choice)) throw new Error(`Jev returned an unavailable choice: ${choice}`);
    if (choice === 'done') {
      if (visited.size !== 3) throw new Error('Jev selected done before every required source was observed.');
      console.log({ status: 'complete', steps: step + 1, visited: [...visited] });
      break;
    }
    const selected = candidates.find(candidate => candidate.id === choice);
    const result = await session.action({ action: 'exec', command: `curl -fsS ${JSON.stringify(selected.url)}` });
    if (result.exit_code !== 0) throw new Error(result.stderr || `Could not fetch ${selected.url}`);
    visited.add(choice);
    console.log({ step: step + 1, choice, bytes: Buffer.byteLength(result.stdout) });
  }
  if (visited.size !== 3) throw new Error(`Bounded loop ended after ${maxSteps} steps with ${visited.size}/3 sources visited.`);
} finally {
  session.close();
}

async function choose(available, history) {
  if (!process.env.TYPESAFE_API_KEY) {
    // Offline mode keeps side effects and the bound testable without credentials.
    return available.find(candidate => candidate.id !== 'done')?.id || 'done';
  }
  const criteria = Object.fromEntries(available.map(candidate => [candidate.id, candidate.description]));
  const response = await fetch('https://api.typesafe.ai/v1/systemone', {
    method: 'POST',
    headers: { authorization: `Bearer ${process.env.TYPESAFE_API_KEY}`, 'content-type': 'application/json' },
    body: JSON.stringify({
      model: process.env.TYPESAFE_MODEL || 'jev-latest',
      state: { goal: 'Fetch each source exactly once, then finish.', visited: [...history] },
      questions: {
        next_action: {
          type: 'choice',
          criteria,
          instructions: 'Choose one available source that has not been visited. Choose done only after all sources are visited.',
        },
      },
    }),
    signal: AbortSignal.timeout(25_000),
  });
  if (!response.ok) throw new Error(`TypeSafe returned HTTP ${response.status}; no action was executed.`);
  const payload = await response.json();
  const answer = payload.answers?.next_action;
  if (!answer || !criteria[answer.choice]) throw new Error('TypeSafe returned an invalid choice; no action was executed.');
  return answer.choice;
}
