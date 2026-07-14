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

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "playbook": PLAYBOOK_SCHEMA,
        "winning": {"type": "array", "items": WINNING_CONCEPT_SCHEMA},
    },
    "required": ["playbook", "winning"],
}
