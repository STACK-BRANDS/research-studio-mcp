# Research Studio MCP — RUNBOOK (Phase 1)

Local setup for Ruben's machine + the Phase-1 validation protocol. Keys stay on your machine.

---

## Part 1 — Local setup (Claude Desktop)

### 1. Clone + install
```bash
cd ~/Projects   # or wherever you keep repos
git clone https://github.com/STACK-BRANDS/research-studio-mcp.git
cd research-studio-mcp
./install.sh
```
✅ Success: a `venv/` folder exists and the script prints an MCP config block ending with the
path to `mcp_server.py`.

### 2. Note your project path
```bash
pwd
```
Copy the output (call it `PROJECT_PATH`). You need it in step 3.

### 3. Register the MCP in Claude Desktop
Open the Claude Desktop config:
`~/Library/Application Support/Claude/claude_desktop_config.json`

Add this server (put your real keys in the `env` block — this is why we use `env` and not the
`.env` file: Claude Desktop launches the process from an unknown working dir, so the keys must be
passed explicitly). Replace `PROJECT_PATH` and the two keys:

```json
{
  "mcpServers": {
    "fb_ad_library": {
      "command": "PROJECT_PATH/venv/bin/python",
      "args": ["PROJECT_PATH/mcp_server.py"],
      "env": {
        "SCRAPECREATORS_API_KEY": "your_scrapecreators_key",
        "GEMINI_API_KEY": "your_gemini_key_optional"
      }
    }
  }
}
```
Drop the `GEMINI_API_KEY` line if you are not doing video analysis yet.

### 4. Restart Claude Desktop
Fully quit and reopen. The `fb_ad_library` tools should appear in the tools menu.

✅ **Success signal:** in a new chat, ask: *"Use get_meta_platform_id for MeUndies, then
get_meta_ads for it, limit 10."* You should get back real, current ads with media URLs and
dates. If you get an auth error, the key isn't reaching the process — recheck the `env` block.

---

## Part 2 — Validation protocol (spec §6)

Goal: prove the source + our prompt-pack produce a report worth trusting, on an objective bar —
not "looks good". Do this once before we build Phase 2.

### Sample
Three competitors from the seed list, ~15-20 currently-active ads each:
- MeUndies (meundies.com) — the mainstream benchmark
- Lace & Lush (laceandlush.com)
- Secret Coco (secretcoco.com)

Add one deliberately different advertiser of your choice (a non-obvious or adjacent brand) to
fight selection bias.

### Run
For each competitor, in a fresh chat: paste `prompts/00-competitor-playbook.md`, then
`prompts/01-winning-signal.md`, then (once, across them) `prompts/02-gap-vs-mv.md`. Fill the
`{{...}}` fields.

### Score (fill this table)
For each competitor, do a quick manual skim of the same ads yourself, then judge the tool's
output:

| competitor | # hook/offer/angle labels | # judged correct | % correct | # non-obvious insights the tool surfaced that your skim missed |
|---|---|---|---|---|
| MeUndies | | | | |
| Lace & Lush | | | | |
| Secret Coco | | | | |
| (your wildcard) | | | | |

Optional second-model cross-check: paste the tool's playbook + a few of the raw ads into a
different model and ask it to flag any label it disagrees with.

### Pass / fail gate
- **PASS** = ≥80% correct labels across the sample AND ≥3 non-obvious insights per competitor.
  → Phase 1 is GO. Open the Phase 2 plan.
- **FAIL** = below either bar. → Note which prompt (`00`/`01`/`02`) produced the weak output,
  iterate that prompt, re-run. Do not proceed to Phase 2.

### Record the verdict
Write the outcome (GO / iterate + what to fix) at the bottom of this file and commit it, so the
Phase-2 decision is traceable.

---

## Notes
- Credit burn: each `get_meta_ads` call reports `credit_info`/`count`. The free tier is 100
  credits — enough for a first pass; upgrade to Freelance ($47) if it runs dry.
- This MCP is read-only. It never posts, buys, or touches an ad account.
