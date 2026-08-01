"""JSON schema for the Research Studio analysis output. Every object —
including nested ones — sets additionalProperties: false and lists every key
it defines in required, per the Anthropic structured-output strict-schema rule.

Per-ad rows carry an `angle_key` (v2 spec §3.1/§4.3) constrained to the
versioned angle registry (`research_angle_registry`, migration 031). Because
that enum must reflect the LIVE registry (a new angle added, one deprecated),
`ANALYSIS_SCHEMA` is not a static dict here -- `build_analysis_schema()`
builds it fresh, querying the registry at call time via the existing
Supabase client. `worker.analyze.analyze()` calls it once per run, right
alongside the registry read that also feeds the analysis prompt.
"""
import logging

logger = logging.getLogger(__name__)

# Fallback registry snapshot, used ONLY when the live `research_angle_registry`
# query fails (table not yet migrated, network blip, whatever) -- schema
# construction (and therefore the analysis call) must never break because the
# registry is unreachable. Mirrors the 11 active rows seeded in
# supabase/migrations/031_rs_rollups.sql (command-center); keep in sync if
# that seed changes. Carries label/definition too (not just the key) since
# worker.analyze also uses this for the prompt's registry block on the same
# failure path.
FALLBACK_ANGLE_REGISTRY = [
    {"angle_key": "fit_curvy", "label": "Fit / curvy",
     "definition": "Fit inclusivity and curvy-body-specific product claims."},
    {"angle_key": "gifting_calendar", "label": "Gifting calendar",
     "definition": "Calendar/seasonal gifting-occasion angle (holidays, anniversaries, etc.)."},
    {"angle_key": "identity_comeback", "label": "Identity / comeback",
     "definition": "\"Getting back to yourself\" / identity-reclamation framing."},
    {"angle_key": "male_gifting", "label": "Male gifting",
     "definition": "The distinct male-gifting funnel -- men buying for a partner."},
    {"angle_key": "morning_ritual", "label": "Morning ritual",
     "definition": "MV whitespace candidate: framing tied to a morning routine/ritual rather "
                    "than a nighttime occasion."},
    {"angle_key": "nightwear", "label": "Nightwear",
     "definition": "Nightwear-specific occasion and product framing."},
    {"angle_key": "occasion_flip_self_worth", "label": "Occasion-flip self-worth",
     "definition": "Reframing a gifting/occasion moment as a self-worth purchase for the "
                    "buyer themself."},
    {"angle_key": "partner_desire", "label": "Partner desire",
     "definition": "Desirability-for-a-partner framing, distinct from self-worth framing."},
    {"angle_key": "social_scarcity_proof", "label": "Social / scarcity proof",
     "definition": "Social proof combined with scarcity mechanics (reviews, \"selling fast\", "
                    "low-stock cues)."},
    {"angle_key": "versatility_wear_it_out", "label": "Versatility / wear-it-out",
     "definition": "MV whitespace candidate: products framed as versatile enough to wear "
                    "outside the bedroom -- competitors' bedroom-only products cannot follow "
                    "this angle."},
    {"angle_key": "unmapped", "label": "Unmapped",
     "definition": "No registry angle genuinely fits this ad's angle. Use this instead of "
                    "force-fitting -- never a permanent home, just the honest default."},
]


def active_angle_registry() -> list[dict]:
    """Query `research_angle_registry` for status='active' rows (angle_key,
    label, definition). Falls back to `FALLBACK_ANGLE_REGISTRY` on ANY
    failure -- must never break schema construction or the analysis call.
    Always guarantees an 'unmapped' entry is present (appended if the live
    query somehow omits it), since per-ad rows fall back to it when nothing
    else fits.
    """
    try:
        # Local import: worker.store pulls in the supabase client, which
        # worker.schema otherwise has no need to import at module load time
        # (e.g. for callers that only want FALLBACK_ANGLE_REGISTRY). No
        # import cycle risk -- worker.store never imports worker.schema.
        from worker.store import _client
        sb = _client()
        res = (
            sb.table("research_angle_registry")
            .select("angle_key,label,definition")
            .eq("status", "active")
            .execute()
        )
        entries = [
            {
                "angle_key": r["angle_key"],
                "label": r.get("label") or r["angle_key"],
                "definition": r.get("definition") or "",
            }
            for r in (res.data or [])
            if r.get("angle_key")
        ]
        if not entries:
            raise ValueError("registry query returned zero active rows")
    except Exception as exc:  # noqa: BLE001 -- must never break schema construction
        logger.warning(
            "active_angle_registry: could not query research_angle_registry, "
            "falling back to the hardcoded seed mirror (%s)", exc,
        )
        entries = [dict(e) for e in FALLBACK_ANGLE_REGISTRY]

    if not any(e["angle_key"] == "unmapped" for e in entries):
        entries.append(dict(FALLBACK_ANGLE_REGISTRY[-1]))  # the 'unmapped' entry
    return sorted(entries, key=lambda e: e["angle_key"])


def angle_keys_from_registry(entries: list[dict]) -> list[str]:
    """Extract just the angle_key strings, for the schema enum."""
    return [e["angle_key"] for e in entries]


def _build_per_ad_schema(angle_keys: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ad_id": {"type": "string"},
            "hook": {"type": "string"},
            "offer": {"type": "string"},
            "angle": {"type": "string"},
            "angle_key": {"type": "string", "enum": angle_keys},
            "days_active": {"type": "integer"},
            "key_visual": {"type": "string"},
        },
        "required": ["ad_id", "hook", "offer", "angle", "angle_key", "days_active", "key_visual"],
    }


def _build_playbook_schema(angle_keys: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "per_ad": {"type": "array", "items": _build_per_ad_schema(angle_keys)},
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


def build_analysis_schema(angle_keys: list[str] | None = None) -> dict:
    """Build the analysis output schema. `angle_keys` constrains each per-ad
    row's `angle_key` enum; when omitted, queries the live registry (with
    the same fallback as `active_angle_registry()`). Callers that already
    fetched the registry entries for the prompt (worker.analyze) should pass
    the derived keys through rather than triggering a second query.
    """
    if angle_keys is None:
        angle_keys = angle_keys_from_registry(active_angle_registry())
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "playbook": _build_playbook_schema(angle_keys),
            "winning": {"type": "array", "items": WINNING_CONCEPT_SCHEMA},
            # Research Studio proposing its own next steps — only when deeper research
            # would materially improve the picture. Empty when the analysis is sufficient.
            "proposed_research": {"type": "array", "items": PROPOSED_RESEARCH_SCHEMA},
        },
        "required": ["playbook", "winning", "proposed_research"],
    }
