from anthropic import Anthropic
from worker.config import settings
from worker.schema import ANALYSIS_SCHEMA

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
    resp = client.messages.create(
        model=settings.model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    import json
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)
