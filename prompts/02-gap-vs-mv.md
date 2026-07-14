# 02 — Gap analysis vs Maison Verdelle

**Run after** `00` and `01` for one or more competitors. **Fill in:** `{{MV_PAGE}}` (Maison
Verdelle's Meta brand name, e.g. "Maison Verdelle"), `{{COMPETITORS}}` (the ones you already
analyzed).

---

Compare **{{COMPETITORS}}**' playbooks against Maison Verdelle's own live ads and produce a
ranked list of gaps MV can test.

## Steps

1. Pull MV's own live ads: `get_meta_platform_id` for "{{MV_PAGE}}" → `get_meta_ads` (limit 40,
   trim=false). If MV has few/no live ads, say so — the gap list then reads as "what competitors
   do that MV isn't running at all".
2. Build MV's current playbook the same way as prompt 00 (hooks, offers, angles, audience,
   objection handling), briefly.
3. Diff the two.

## Output

### A. What competitors do that MV does not
Ranked by how strongly it showed up as a *likely winning* concept in prompt 01. Each row:

| gap | which competitor(s) + evidence (ad_id / hook) | why it likely works | how MV could test it (one concrete creative idea) | priority (H/M/L) |

### B. What MV does that competitors do not
Angles/offers MV owns — protect or lean into these.

### C. Whitespace
Angles NO ONE in the set is running that fit MV's category and audience. Label clearly as
hypothesis (nobody has validated these), lower confidence than A.

## Rules
- Priority in section A follows the confidence from prompt 01: only concepts that were
  high/medium "likely winning" get High priority. A gap resting on a single long-running ad is
  Low.
- Every gap cites the competitor ad(s) behind it.
- Frame every item as a testable idea ("test X"), never as a guaranteed win.
