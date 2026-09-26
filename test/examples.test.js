import assert from 'node:assert/strict';
import test from 'node:test';
import { MolaClient, MolaApiError } from '../examples/lib/mola-client.mjs';
import { scoreFixtureReport } from '../examples/benchmarks/social-media-report/runner/score.mjs';
import expected from '../examples/benchmarks/social-media-report/rubric/expected.json' with { type: 'json' };
import { createEngine } from '../src/mcp/engine.js';

test('remote MCP uses the explicit environment token', () => {
  const previous = process.env.MOLA_TOKEN;
  process.env.MOLA_TOKEN = 'remote-token';
  try { assert.equal(createEngine().hasToken(), true); }
  finally {
    if (previous === undefined) delete process.env.MOLA_TOKEN;
    else process.env.MOLA_TOKEN = previous;
  }
});

test('example client unwraps data and reports API errors with status', async () => {
  const calls = [];
  const client = new MolaClient({
    token: 'test',
    api: 'http://mola.test/v1/',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return new Response(JSON.stringify({ data: { id: 'machine-1' } }), { status: 201 });
    },
  });
  assert.deepEqual(await client.createMachine({ name: 'test' }), { id: 'machine-1' });
  assert.equal(calls[0][0], 'http://mola.test/v1/machines');
  assert.equal(calls[0][1].headers.authorization, 'Bearer test');

  const failing = new MolaClient({
    token: 'test',
    fetchImpl: async () => new Response(JSON.stringify({ message: 'No capacity.' }), { status: 409 }),
  });
  await assert.rejects(() => failing.createMachine({}), error =>
    error instanceof MolaApiError && error.status === 409 && error.message === 'No capacity.');
});

test('fixture scorer requires claim-local citations and five distinct actions', async () => {
  const source = name => `http://127.0.0.1:8765/${name}`;
  const report = `# Report
## Major themes
- Rover Mini and quiet motors are central. ${source('launch.html')}
## Notable discussions
- Linux SDK interest accompanies 1,240 likes. ${source('launch.html')}
## Recurring praise
- Quiet motors are praised. ${source('launch.html')}
## Recurring complaints
- Replacement parts and Wi-Fi reconnect failures recur. ${source('support.html')}
## Visible engagement
- The launch has 1,240 likes, 188 reposts, and 94 replies. ${source('launch.html')}
- Support has 86 replies and 310 likes. ${source('support.html')}
## Competitive mentions
- BetaBot Pocket is associated with longer battery life. ${source('market.html')}
## Actionable observations
1. Publish the Linux SDK timeline. ${source('launch.html')}
2. Stock replacement parts. ${source('support.html')}
3. Fix Wi-Fi reconnect. ${source('support.html')}
4. Emphasize quiet motors. ${source('launch.html')}
5. Compare battery life carefully. ${source('market.html')}
`;
  const score = await scoreFixtureReport(report, expected);
  assert.equal(score.passed, true);
  assert.equal(score.required_sections, 1);
  assert.equal(score.supported_claim_ratio, 1);
  assert.equal(score.claim_citation_ratio, 1);
  assert.equal(score.actionable_observations, 1);
  assert.deepEqual(score.unsupported_numeric_claims, []);
});
