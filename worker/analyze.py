from anthropic import Anthropic
from worker import spend_guard
from worker.config import settings
from worker.schema import ANALYSIS_SCHEMA
from worker.usage_reporter import UsageReporter

# Fleet usage reporter (constructed once per process). No-op unless
# USAGE_INGEST_URL/USAGE_INGEST_TOKEN are set -- never raises, never blocks
# analyze(). worker.run.main() flushes this at the end of every run.
usage = UsageReporter(system="research", repo="research-studio-mcp")

SYSTEM = (
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
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
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
