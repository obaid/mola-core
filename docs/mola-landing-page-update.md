# Mola Landing Page Update - November 9, 2026

## Task Summary
Update mola.sh landing page with new positioning focusing on persistent, signed-in computers for always-on agents, differentiating from ephemeral sandbox competitors like boat.dev.

## Research Findings

### Current State Analysis

**Hyperwake Branding Found:**
- GitHub repo description: "Omarchy computers on your own machine, driven over a REST API. The open-source Hyperwake engine."
- GitHub homepage URL: `https://hyperwake.ai/`
- These need to be updated to use Mola branding

**Landing Page Status (site/index.html):**
- Already uses Mola branding consistently
- Current positioning: "Cloud computers for you and your agents"
- Generic, doesn't differentiate from competitors

### Features Verified in Codebase

**✅ CONFIRMED Features:**
1. **Ubuntu desktops** - Documented in `docs/ubuntu-desktops.md`, Ubuntu 24.04 XFCE + Chrome
2. **Persistence** - Machines have persistent disks, stop/start without data loss
3. **Live desktop view** - VNC-based desktop streaming with embed support
4. **Credential vault** - `vault-inject` API endpoint for secure login automation (docs/guest-tools.md)
5. **REST API** - Full API documented in `docs/api.md`
6. **MCP support** - Documented in `docs/mcp.md`, works with Claude Code/Codex
7. **Open source** - FSL-1.1-ALv2 license, self-hostable via `npx mola-core`

**❌ UNVERIFIED/NOT SUPPORTED:**
1. **Stable addresses** - README states: "IP changes each resume" - NOT a stable address feature
2. **Pricing** - Only visible on cloud.mola.sh (not in repo), cannot verify exact pricing tiers
3. **provision.ai** - Not mentioned anywhere in codebase
4. **mailboxkit.com** - Not mentioned anywhere in codebase
5. **Omarchy-only claim** - Ubuntu is also supported (24.04 XFCE)

### Competitor Context from Research
- boat.dev: $0.036/h for 4vCPU/8GB, ephemeral VMs, IP changes on resume, EU-only
- Modal, E2B, Daytona: Multi-hundred million dollar businesses in ephemeral sandbox space
- Market positioning: Mola should emphasize **persistence**, **credential management**, and **long-running agent workflows**

## Implementation Plan

### 1. Landing Page Updates (site/index.html)
- **Hero section**: Clear differentiation vs ephemeral sandboxes
- **Headline**: Focus on "persistent" and "always-on" agents
- **Key benefits**:
  - Persistent disk across sessions
  - Credential vault for logins
  - Live desktop view
  - Works with Claude Code/Codex/OpenClaw
- **Honest comparison**: vs ephemeral sandboxes (no invented numbers)
- **Quickstart**: Real API example (already present, verify accuracy)
- **CTAs**: cloud.mola.sh and self-host

### 2. Content Sections
- Keep existing design system
- Update copy to emphasize:
  - "Keep your computer" vs "start fresh every time"
  - "Signed-in sessions" vs "configure from scratch"
  - "Long-running agents" vs "ephemeral tasks"

### 3. GitHub Repo (Cannot update directly, but will note in report)
- Description needs: "Persistent Linux desktops (Ubuntu/Omarchy) driven over a REST API. Self-host or use Mola Cloud."
- Homepage URL needs: `https://mola.sh/`

### 4. Testing
- Run build (no build script found, but verify HTML is valid)
- Check for broken links
- Take before/after screenshots

## Implementation Status

### ✅ Completed
1. ✅ Created feature branch: `cursor/mola-landing-positioning-8278`
2. ✅ Updated site/index.html with new positioning
3. ✅ Tests pass (npm test)
4. ✅ Added HTML comment documenting changes
5. Next: Push and create PR

### Landing Page Changes Made

**Title & Meta Tags:**
- Title: "Mola | Persistent computers for always-on agents"
- Description emphasizes: persistent, AI agents, logins/files survive restarts, works with Claude Code/Codex/OpenClaw

**Hero Section:**
- Headline: "Persistent computers for always-on agents"
- Subheading: Full Linux desktops (Ubuntu or Omarchy), REST API, permanent disks, credential vault, sessions survive restarts

**Workspace Section:**
- New heading: "Not an ephemeral sandbox. A real computer that stays."
- Explicitly contrasts with ephemeral sandboxes
- Features: Credential vault for saved logins, Live view of desktop

**Agent Section:**
- Heading: "Works with Claude Code, Codex, and OpenClaw-style agents"
- Positioned for: coding agents, long-running assistants, agent fleets

**Open Source Section:**
- Heading: "Open-source core. Self-host on your hardware"
- Emphasizes FSL-1.1-ALv2 license, no vendor lock-in

## Features Could NOT Verify from Codebase
1. **Stable addresses** - README explicitly states "IP changes each resume"
2. **Exact cloud pricing** - Only visible on cloud.mola.sh, not in this repo
3. **provision.ai** - Not mentioned in codebase
4. **mailboxkit.com** - Not mentioned in codebase

## Hyperwake Branding Found
**GitHub Repository (cannot update from here):**
- Description: "Omarchy computers on your own machine, driven over a REST API. The open-source Hyperwake engine."
- Homepage: https://hyperwake.ai/
- **Recommendation**: Update to "Persistent Linux desktops (Ubuntu/Omarchy) driven over a REST API. Self-host or use Mola Cloud." with homepage https://mola.sh/

**Landing Page & README:**
- ✅ Already use Mola branding consistently
- No Hyperwake references found in site/index.html or README.md
