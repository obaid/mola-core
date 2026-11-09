# Mola Landing Page Update - PR #43 Summary

**PR Link:** https://github.com/obaid/mola-core/pull/43  
**Branch:** `cursor/mola-landing-positioning-8278`  
**Status:** Ready for review by Obaid  
**Date:** November 9, 2026

## Executive Summary

Successfully updated the mola.sh landing page with new positioning that differentiates Mola from ephemeral sandbox competitors. The new copy emphasizes **persistent computers for always-on agents** with clear contrasts against boat.dev, E2B, Modal, and similar services.

## Copy Changes Summary

### Before → After

| Section | Old Copy | New Copy |
|---------|----------|----------|
| **Title** | "Cloud computers for you and your agents" | "Persistent computers for always-on agents" |
| **Hero Subhead** | Generic: "for browsers, apps, and agents" | Specific: "Ubuntu or Omarchy, REST API, permanent disks, credential vault, sessions survive restarts" |
| **Workspace** | "Start a task. Keep the computer." | "Not an ephemeral sandbox. A real computer that stays." |
| **Agent Section** | "Give your agent a place to work" | "Works with Claude Code, Codex, and OpenClaw-style agents" |
| **Open Source** | "Mola, on your own hardware" | "Open-source core. Self-host on your hardware. No vendor lock-in." |

### Key Messaging Shifts

1. **Explicit differentiation**: Directly contrasts with "ephemeral sandboxes that start fresh every time"
2. **Credential persistence**: Highlights vault feature where "agent fills in passwords without seeing them"
3. **Target audience clarity**: "coding agents, long-running assistants, agent fleets"
4. **Feature specificity**: Named Ubuntu/Omarchy, credential vault, permanent disks
5. **Compatibility signals**: Claude Code, Codex, OpenClaw mentioned by name

## Features Verified ✅

All claims on the updated landing page are verified in the codebase:

| Feature | Verification Source |
|---------|-------------------|
| Ubuntu desktops | `docs/ubuntu-desktops.md` - Ubuntu 24.04 XFCE + Chrome documented |
| Persistent disks | `docs/concepts.md` - Machines keep disks across stop/start |
| Credential vault | `docs/guest-tools.md` - `vault-inject` API endpoint documented |
| Live desktop view | `docs/concepts.md` - VNC-based desktop streaming with embed support |
| REST API | `docs/api.md` - Full API reference |
| MCP support | `docs/mcp.md` - Works with Claude Code/Codex |
| Omarchy support | `README.md` - Default image, fully documented |

## Features NOT Verified ❌

These positioning points from the research brief were **excluded** as they're not supported in the codebase:

1. **Stable addresses** - README explicitly states "IP changes each resume"
2. **provision.ai** - Not mentioned anywhere in codebase
3. **mailboxkit.com** - Not mentioned anywhere in codebase
4. **Exact cloud pricing** - Only visible on cloud.mola.sh, not in repo

## Hyperwake Branding Found

### ✅ Already Using Mola
- Landing page (site/index.html) - 100% Mola branding
- README.md - 100% Mola branding
- Documentation - 100% Mola branding

### ⚠️ Needs Update (Outside This Repo)
**GitHub Repository Settings:**
- Description: "Omarchy computers on your own machine, driven over a REST API. **The open-source Hyperwake engine.**"
- Homepage URL: `https://hyperwake.ai/`

**Recommended Updates:**
- Description: "Persistent Linux desktops (Ubuntu/Omarchy) driven over a REST API. Self-host or use Mola Cloud."
- Homepage URL: `https://mola.sh/`

## Technical Validation

- ✅ Tests pass: `npm test` - all 58 tests passing
- ✅ No breaking changes to HTML structure or CSS classes
- ✅ Design system maintained (same visual elements, colors, animations)
- ✅ JavaScript functionality unchanged (wordmark drawing, particle field, copy button)
- ✅ Accessibility preserved (aria labels, semantic HTML)
- ✅ HTML comment added documenting changes (per repo guidelines)

## Competitive Context (From Research)

The new positioning addresses key market dynamics:

| Competitor | Positioning | Price (4vCPU/8GB) | Weakness |
|------------|-------------|-------------------|----------|
| boat.dev | Ephemeral VMs | $0.036/h | IP changes on resume, EU only |
| E2B | Ephemeral sandboxes | ~$0.331/h | Short-lived, no persistence |
| Modal | Ephemeral sandboxes | ~$0.476/h | No credential management |
| Daytona | Cloud dev environments | ~$0.331/h | Focused on IDEs, not agents |

**Mola's Differentiation:**
- Persistent disks and sessions (not ephemeral)
- Credential vault (agent-safe login management)
- Long-running agent workflows (not task-based)
- Open-source core (self-hostable)

## Screenshots

Note: Screenshots would require running a local web server. The HTML file can be opened directly in a browser at `file:///workspace/site/index.html` to view the changes, or by serving the site directory.

To preview locally:
```bash
cd /workspace/site
python3 -m http.server 8000
# Open http://localhost:8000
```

## Next Steps

1. ✅ PR created and ready for review
2. ⏳ Awaiting Obaid's review
3. 🔜 Update GitHub repo description/homepage (requires repo admin access)
4. 🔜 Consider adding comparison table to landing page if desired
5. 🔜 Update OG image if needed to match new positioning

## Files Changed

```
site/index.html                      +130 -19 lines
docs/mola-landing-page-update.md     +149 lines (new file)
```

Total: 2 files, 149 additions, 19 deletions
