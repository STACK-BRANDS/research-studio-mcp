from anthropic import Anthropic
from worker.config import settings
from worker.schema import ANALYSIS_SCHEMA

SYSTEM = (
    "You are a paid-social analyst for Maison Verdelle (lingerie/intimates). "
    "Analyze a competitor's live Meta ads from the data provided. Cite ad_id or a "
    "verbatim hook for every claim. 'Likely winning' is a confidence-scored hypothesis: "
    "concept duplication ('doubling down') is the strongest signal, longevity only supports "
    "it — never state performance as fact."
)

def analyze(brand: str, distinct_ads: list[dict], images: list[tuple[str, bytes, str]]) -> dict:
    import base64
    client = Anthropic(api_key=settings.anthropic_api_key)
    content = [{"type": "text",
                "text": f"Competitor: {brand}\nDistinct ads (JSON):\n{distinct_ads}"}]
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
