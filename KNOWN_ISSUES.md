# Known issues

## OPEN — analyze_ad_video: Gemini model 404 (video analysis unusable)

**Symptom:** `analyze_ad_video` fails with HTTP 404 "model not found" on every call
(confirmed on ad_id `4266563910300310`, MeUndies, 2026-07-14). Image + copy analysis work;
video is the only broken tool.

**Root cause:** upstream hardcoded `GEMINI_MODEL = "gemini-2.0-flash-exp"` (retired experimental
model). Changed to `gemini-2.0-flash` (env-overridable via `GEMINI_MODEL`), but that name is also
rejected on the current API/client version in use.

**Already done:** `src/services/gemini_service.py` →
`GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")` (switchable without a code change).

**To fix (needs the real Gemini key — do it on Ruben's machine):**
1. Try candidate models via the Claude Desktop config `env` block, one at a time, restarting and
   re-testing ad `4266563910300310`: `gemini-2.5-flash`, `gemini-1.5-flash`, `gemini-1.5-pro`.
2. If all 404, check the `google-genai` client version in `requirements.txt` — an old client can
   pin a `v1beta` endpoint that doesn't serve newer models; bump it and retry.
3. Verify the Files API video-upload path (`upload_video_to_gemini`) still matches the current
   client API.

**Acceptance:** `analyze_ad_video` on ad `4266563910300310` returns a structured visual readout
(not a 404), and the working model name becomes the default in `gemini_service.py`.

**Priority:** NOT blocking Phase-1 validation (video = 1 of 14 creatives; copy-only fallback
works), but MUST be fixed before Phase 2 / before treating video creatives as covered.
