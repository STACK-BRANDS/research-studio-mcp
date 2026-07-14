# 01 — "Likely winning" signal (composite, confidence-scored)

**Run after** `00-competitor-playbook.md` for the same competitor, in the same chat (so the
retrieved ads are in context). **Fill in:** `{{COMPETITOR}}`.

---

Ad longevity alone is a WEAK proxy for performance: a long-running ad can be low-spend,
evergreen, a retargeting creative, geo-reuse, or an artifact of incomplete scraper history. So
do NOT rank by longevity. Score which of **{{COMPETITOR}}**'s creatives are *likely* working
using the composite below, and always attach a confidence level.

## Signals (in priority order)

1. **Concept duplication / "doubling down" (STRONGEST).** Count how many near-identical
   creative variants of the same concept the brand runs (same hook family, same angle, minor
   swaps). Advertisers duplicate what converts. A concept with many live variants > a lone
   long-runner.
2. **Longevity (supporting).** Days active, ranked relative to the set — not absolute. Use only
   to support a concept that already shows duplication.
3. **Refresh cadence (supporting).** How fast they iterate a concept (many recent start-dates on
   one concept = active scaling).
4. **Spend / impression range (supporting, when the tool exposes it).** Use if present in the ad
   metadata; otherwise say it was unavailable.

## Output

A ranked "likely winning concepts" table, strongest first:

| rank | concept (hook family + angle) | # live variants | longevity (relative) | refresh signal | confidence (high/med/low) | signals behind the call |

Rules:
- **Confidence = high** only when concept-duplication is clear AND at least one supporting signal
  agrees. **Medium** when duplication is moderate or signals are mixed. **Low** when the call
  rests mostly on longevity or a thin sample.
- Never write "this ad is winning" as fact. Write "likely winning (confidence: X) because ...".
- End with the 3-5 concepts you would bet are their current winners, and explicitly flag any
  where you are guessing.
