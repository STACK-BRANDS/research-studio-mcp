# Known issues

## RESOLVED (2026-07-14) — analyze_ad_video: Gemini model 404

**Was:** `analyze_ad_video` failed with 404 on every call. Upstream hardcoded
`gemini-2.0-flash-exp` (retired); `gemini-2.0-flash` and `gemini-2.5-flash` also came back
unavailable ("no longer available to new users") on our key.

**Fix:** `src/services/gemini_service.py` no longer hardcodes a model. `generate_with_fallback()`
walks a candidate chain (`GEMINI_MODEL` env override first, then `gemini-2.5-flash-002` →
`gemini-2.0-flash-001` → `gemini-2.0-flash` → `gemini-flash-latest` → `gemini-1.5-flash-002` →
`gemini-1.5-flash`) and caches the first that works on the key. Both single and batch video
paths use it. Self-heals when Google rotates model names again.

**Verified:** video analysis returned a full readout for MeUndies ad `4266563910300310`
(Minions "MatchMe" video) — scenes, text overlay, colorways, end card, CTA. No 404.

_No open issues._
