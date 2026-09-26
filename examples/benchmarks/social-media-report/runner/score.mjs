const normalize = value => value.toLowerCase().replace(/[^a-z0-9./:-]+/g, ' ').trim();

export async function scoreFixtureReport(report, expected) {
  const sectionHits = expected.required_sections.filter(section => new RegExp(`(^|\\n)#{1,4}[^\\n]*${section}`, 'i').test(report));
  const claimChecks = expected.claims.map(claim => {
    const fact = normalize(claim.fact);
    const lines = report.split('\n').filter(line => normalize(line).includes(fact));
    const cited = lines.some(line => claim.sources.some(source => normalize(line).includes(normalize(source))));
    return { fact: claim.fact, present: lines.length > 0, cited };
  });
  const present = claimChecks.filter(item => item.present);
  const actionableBlock = report.match(/#{1,4}[^\n]*actionable[^\n]*\n([\s\S]*?)(?=\n#{1,4}|$)/i)?.[1] || '';
  const actions = actionableBlock.split('\n').filter(line => /^\s*(?:[-*]|\d+[.)])\s+/.test(line));
  const uniqueActions = new Set(actions.map(normalize));
  const allowedNumbers = new Set(expected.allowed_numbers.map(String));
  const proseWithoutListNumbers = report
    .replace(/https?:\/\/[^\s)\]]+/g, '')
    .replace(/^\s*\d+[.)]\s+/gm, '');
  const unsupportedNumbers = [...new Set(proseWithoutListNumbers.match(/\b\d[\d,]*(?:\.\d+)?%?\b/g) || [])]
    .filter(value => !allowedNumbers.has(value.replace(/%$/, '')) && !['5'].includes(value));
  const citations = report.match(/https?:\/\/[^\s)\]]+/g) || [];
  const validCitations = citations.filter(url => expected.sources.some(source => url.includes(source)));
  return {
    evaluated: true,
    required_sections: sectionHits.length / expected.required_sections.length,
    supported_claim_ratio: present.length / expected.claims.length,
    claim_citation_ratio: present.length ? present.filter(item => item.cited).length / present.length : 0,
    citation_validity: citations.length ? validCitations.length / citations.length : 0,
    actionable_observations: Math.min(uniqueActions.size / expected.actionable_observations, 1),
    duplicate_actionable_observations: actions.length - uniqueActions.size,
    unsupported_numeric_claims: unsupportedNumbers,
    passed: sectionHits.length === expected.required_sections.length
      && present.length === expected.claims.length
      && present.every(item => item.cited)
      && uniqueActions.size >= expected.actionable_observations
      && unsupportedNumbers.length === 0,
    claims: claimChecks,
  };
}
