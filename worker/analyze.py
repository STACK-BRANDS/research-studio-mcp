from anthropic import Anthropic
from worker import spend_guard
from worker.config import settings
from worker import schema
from worker.usage_reporter import UsageReporter

# Fleet usage reporter (constructed once per process). No-op unless
# USAGE_INGEST_URL/USAGE_INGEST_TOKEN are set -- never raises, never blocks
# analyze(). worker.run.main() flushes this at the end of every run.
usage = UsageReporter(system="research", repo="research-studio-mcp")

SYSTEM_BASE = (
    "You are a paid-social analyst for Maison Verdelle (lingerie/intimates). "
    "Analyze a competitor's live Meta ads from the data provided. Cite ad_id or a "
    "verbatim hook for every claim. 'Likely winning' is a confidence-scored hypothesis: "
    "concept duplication ('doubling down') is the strongest signal, longevity only supports "
    "it — never state performance as fact.\n"
    "You are shown a SAMPLE (freshest + longest-running) of the competitor's ads, not "
    "necessarily all of them. Populate `proposed_research` ONLY when deeper research would "
    "materially improve the picture — each item is one concrete topic + a one-line rationale "
    "(why it matters for Maison Verdelle) + a kind. Examples: a high-volume advertiser you "
    "clearly saw a thin slice of → deeper_ad_pull; winners you can only rate low-confidence "
    "for lack of reach/spend signal → reach_deepdive; unclear painpoints/desires/objections "
    "→ voc_reddit; enough competitor picture to compare against MV → gap_analysis; something "
    "needing MV's own ads/CX/performance → own_store; adjacent competitors worth adding → "
    "competitor_discovery. Leave it empty when the analysis is already sufficient — do not "
    "invent busywork."
)

# Appended to SYSTEM_BASE, with the live registry block filled in per call
# (worker.schema.active_angle_registry() -- same registry snapshot that also
# constrains the structured-output schema's angle_key enum, so the prompt
# instruction and the enum can never drift apart within one analyze() call).
ANGLE_KEY_INSTRUCTIONS = (
    "\n\nEvery per-ad row must also set `angle_key`: the single best-matching key from the "
    "versioned angle registry below, chosen using each entry's label and definition as the "
    "match guide. `angle` stays your own free-text description of the ad's angle; `angle_key` "
    "is the canonical mapping alongside it. Use `unmapped` only when the ad's angle genuinely "
    "does not fit any registry entry — never force-fit an ad into a close-but-wrong angle just "
    "to avoid unmapped.\n\n"
    "Angle registry:\n{registry_block}"
)


def _angle_registry_block(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        if e["angle_key"] == "unmapped":
            continue
        definition = f" — {e['definition']}" if e.get("definition") else ""
        lines.append(f"- {e['angle_key']} ({e['label']}){definition}")
    return "\n".join(lines)


def _build_system(registry_entries: list[dict]) -> str:
    return SYSTEM_BASE + ANGLE_KEY_INSTRUCTIONS.format(
        registry_block=_angle_registry_block(registry_entries)
    )


def analyze(
    brand: str,
    distinct_ads: list[dict],
    images: list[tuple[str, bytes, str]],
    scraped_count: int | None = None,
) -> dict:
    import base64
    # Spend guard: one Anthropic call per analyze() call. Checked immediately
    # before the API hit, so a capped call spends nothing. Raises
    # ResearchSpendCapExceeded (a BaseException) which must NOT be caught
    # here -- it propagates to worker.run.main's explicit handler.
    spend_guard.guard()
    client = Anthropic(api_key=settings.anthropic_api_key)

    # One registry read feeds BOTH the prompt's angle instructions and the
    # structured-output schema's angle_key enum -- a single Supabase query,
    # not a second Anthropic call. Falls back internally (schema.py) if the
    # registry is unreachable, so this never blocks the analysis.
    registry_entries = schema.active_angle_registry()
    angle_keys = schema.angle_keys_from_registry(registry_entries)
    analysis_schema = schema.build_analysis_schema(angle_keys)
    system = _build_system(registry_entries)

    header = f"Competitor: {brand}\nAnalyzing {len(distinct_ads)} distinct creatives"
    if scraped_count is not None:
        header += f" (sampled from {scraped_count} pulled)"
    content = [{"type": "text",
                "text": f"{header}\nDistinct ads (JSON):\n{distinct_ads}"}]
    for ad_id, raw, mtype in images:
        content.append({"type": "text", "text": f"Image for ad_id {ad_id}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": mtype,
            "data": base64.b64encode(raw).decode()}})
    # Stream with a generous budget: sonnet-5 runs adaptive thinking by default, and
    # max_tokens caps thinking + output together — 16k truncated the JSON on large
    # analyses. 32k leaves room for both; streaming avoids HTTP timeouts at that size.
    with client.messages.stream(
        model=settings.model,
        max_tokens=32000,
        system=system,
        output_config={"format": {"type": "json_schema", "schema": analysis_schema}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()
    # Fleet usage: report tokens right after the call that spent them. The
    # server prices tokens (no client-side pricing table); this call never
    # raises and never affects analyze()'s return, even on a bad response.
    usage.spend(
        action="rs-worker/analyze-competitor",
        model=settings.model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        meta={
            "brand": brand,
            "distinct_ads": len(distinct_ads),
            "scraped_count": scraped_count,
        },
    )
    import json
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)
