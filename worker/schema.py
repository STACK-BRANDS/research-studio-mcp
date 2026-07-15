"""JSON schema for the Research Studio analysis output. Every object —
including nested ones — sets additionalProperties: false and lists every key
it defines in required, per the Anthropic structured-output strict-schema rule.
"""

PER_AD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ad_id": {"type": "string"},
        "hook": {"type": "string"},
        "offer": {"type": "string"},
        "angle": {"type": "string"},
        "days_active": {"type": "integer"},
        "key_visual": {"type": "string"},
    },
    "required": ["ad_id", "hook", "offer", "angle", "days_active", "key_visual"],
}

PLAYBOOK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "per_ad": {"type": "array", "items": PER_AD_SCHEMA},
        "plays": {"type": "array", "items": {"type": "string"}},
        "audience": {"type": "string"},
        "objections": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["per_ad", "plays", "audience", "objections"],
}

WINNING_CONCEPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "concept": {"type": "string"},
        "live_variants": {"type": "integer"},
        "longevity": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "signals": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["concept", "live_variants", "longevity", "confidence", "signals"],
}

PROPOSED_RESEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic": {"type": "string"},              # what to research next, concretely
        "rationale": {"type": "string"},           # one line: why it matters for MV
        "kind": {
            "type": "string",
            "enum": [
                "deeper_ad_pull",        # high-volume advertiser sampled thin → widen
                "reach_deepdive",        # low-confidence winners → Apify EU-reach detail
                "voc_reddit",            # painpoints/desires/objections unclear → Reddit VoC
                "gap_analysis",          # ready to compare vs MV's own plays
                "own_store",             # needs MV first-party data (ads/CX/performance)
                "competitor_discovery",  # find more/adjacent competitors
                "other",
            ],
        },
    },
    "required": ["topic", "rationale", "kind"],
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "playbook": PLAYBOOK_SCHEMA,
        "winning": {"type": "array", "items": WINNING_CONCEPT_SCHEMA},
        # Research Studio proposing its own next steps — only when deeper research
        # would materially improve the picture. Empty when the analysis is sufficient.
        "proposed_research": {"type": "array", "items": PROPOSED_RESEARCH_SCHEMA},
    },
    "required": ["playbook", "winning", "proposed_research"],
}
