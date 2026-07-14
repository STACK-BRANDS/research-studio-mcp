# Research Studio — analyst prompt-pack

This is the layer STACK BRANDS owns. The forked MCP delivers raw ads; these prompts turn
them into judgement. Paste a prompt into a Claude Desktop chat that has the `fb_ad_library`
MCP connected, replacing the `{{...}}` fields.

## Run order

1. **`00-competitor-playbook.md`** — one competitor in, structured playbook out (hooks,
   offers, angles, audience, objection handling), every claim cited to specific ads.
2. **`01-winning-signal.md`** — scores which of that competitor's creatives are *likely*
   working, using a composite, confidence-scored method. Concept-duplication ("doubling
   down") is the strongest signal; longevity only supports it. Never states performance as
   fact.
3. **`02-gap-vs-mv.md`** — compares the competitor's playbook against Maison Verdelle's own
   live ads and returns a ranked "test this" gap list.

## Contract

- Every factual claim about a competitor cites the ad(s) it came from (ad id or a quoted
  hook line). No uncited generalizations.
- "Likely winning" always carries a confidence level (high / medium / low) and the signals
  behind it. This mirrors the Stack Intelligence `origin` vs `validation` split: a
  competitor-origin insight is a hypothesis until corroborated.
- These prompts are the Phase-1 deliverable. Their quality gate is the validation protocol
  in `../RUNBOOK.md` (spec §6). Iterate the prompts here if the gate fails.

## Design brand

Focus brand for v1 = **Maison Verdelle (MV)**, lingerie/intimates. Seed competitors are in
the spec §12. Keep the analysis grounded in that category.
