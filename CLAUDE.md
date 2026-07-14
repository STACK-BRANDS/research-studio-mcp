# research-studio-mcp

**Purpose:** STACK BRANDS Research Studio ingestion layer — a forked Meta Ads Library MCP
for competitive intelligence on selected competitors. It delivers *raw ads*; our own
analyst prompt-pack (`prompts/`) turns them into the playbook. Later feeds the Research
Studio CCC section + a monitor-bot.

**Upstream:** fork of `trypeggy/facebook-ads-library-mcp` (network root `proxy-intell`), MIT.
**Data source:** ScrapeCreators Facebook Ad Library API (works on commercial ads).
**Spec:** `command-center/docs/superpowers/specs/2026-07-14-research-studio-design.md`
**Plan:** `command-center/docs/superpowers/plans/2026-07-14-research-studio-phase1.md`

> Global rules (model routing, guardrails) live in `~/.claude/CLAUDE.md` and always apply.

## Model lane
**Sonnet** executes · **Haiku** reads.

## Keys / secrets
Keys live ONLY in a local `.env` or the Claude Desktop MCP `env` block — never commit them.
`.env` is gitignored. Builders never handle live keys.

## The tools this MCP exposes (upstream)
`get_meta_platform_id(brand_names)` · `get_meta_ads(platform_ids, limit, country, trim)` ·
`analyze_ad_image(...)` · `analyze_ad_video(...)` · `analyze_ad_videos_batch(...)` · media-cache
tools. All read-only.

## Our layer
`prompts/` = the intelligence layer we own (playbook extraction, confidence-scored
"likely winning" signal, gap-vs-MV). Run order and contract in `prompts/README.md`.
