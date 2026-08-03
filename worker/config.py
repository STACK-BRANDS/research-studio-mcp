import os
from typing import Optional

from dotenv import load_dotenv

# Load .env into the environment when the worker config is first imported, so the
# lazy properties below find the keys. No-op if there's no .env (tests / CI).
load_dotenv()


class Settings:
    """Lazy config. Required keys are read on ACCESS, not at import — so importing
    worker.* (tests, tooling) never requires the full server-side env, only actually
    running the worker does."""

    @property
    def anthropic_api_key(self) -> str:
        return os.environ["ANTHROPIC_API_KEY"]

    @property
    def scrapecreators_api_key(self) -> str:
        return os.environ["SCRAPECREATORS_API_KEY"]

    @property
    def supabase_url(self) -> str:
        return os.environ["SUPABASE_URL"]

    @property
    def supabase_service_key(self) -> str:  # sb_secret_ (new key)
        return os.environ["SUPABASE_SERVICE_KEY"]

    @property
    def model(self) -> str:
        return os.getenv("RESEARCH_MODEL", "claude-sonnet-5")

    @property
    def max_images(self) -> int:
        return int(os.getenv("RESEARCH_MAX_IMAGES", "8"))

    @property
    def ad_limit(self) -> int:
        # How many ads to PULL (cheap). Wide, so high-volume advertisers aren't a
        # blind thin slice. The snapshot keeps all of these.
        return int(os.getenv("RESEARCH_AD_LIMIT", "60"))

    @property
    def analysis_cap(self) -> int:
        # How many distinct creatives the (expensive) Claude analysis sees, after a
        # recency+longevity stratified sample of the pulled set.
        return int(os.getenv("RESEARCH_ANALYSIS_CAP", "30"))

    @property
    def job_lease_timeouts(self) -> dict:
        """Per-job_kind stale-claim lease timeout in seconds, used by
        `worker.jobs.reap_stale()`. A worker calls `jobs.heartbeat()` roughly
        every ~30s while a handler runs (architecture §13 R1), so every default
        below is a comfortable multiple of that heartbeat interval -- long
        enough to ride out a slow network hiccup or a GC pause without
        mistaking a live worker for a dead one, short enough that a genuinely
        crashed/killed worker's job is re-queued well within a human's
        patience for a visibly stuck run.

        `scrape` and `synthesize` get the longest budget: `scrape` re-runs
        today's single-shot ads pipeline (a ScrapeCreators pull plus a Claude
        vision analysis over up to `max_images` images), and `synthesize` can
        read a large accumulated evidence set -- both can legitimately run for
        several minutes. `collect` and `verify` are one bounded model call
        over a single capture and complete quickly, so they get a shorter
        leash.

        Override any single kind via `RESEARCH_JOB_LEASE_TIMEOUT_<KIND>`
        (seconds), e.g. `RESEARCH_JOB_LEASE_TIMEOUT_SCRAPE=900`.
        """
        defaults = {
            "scrape": 600,
            "collect": 300,
            "verify": 300,
            "synthesize": 600,
        }
        return {
            kind: int(os.getenv(f"RESEARCH_JOB_LEASE_TIMEOUT_{kind.upper()}", str(default)))
            for kind, default in defaults.items()
        }

    @property
    def price_cards(self) -> dict:
        """Worst-case (never average) cents per (job_kind, connector) -- the
        ceiling `worker.budget.reserve()` prices a paid call at and hands to
        `rs_reserve_spend` as `p_est_cents` (deep-research plan R2). The
        deployed money-migration's contract is: the TRUE actual cost can
        never exceed this reservation, so every card here MUST be priced
        from the call's hard caps (max_tokens, image count, ...), not from a
        typical/average run -- under-pricing silently breaks that guarantee
        and trips `budget.settle()`'s worker-side overspend assertion.

        Keyed by job_kind -> {connector_or_"*": cents}. `"*"` is a job_kind-
        level default used when no connector-specific entry applies (or no
        connector was given); a job_kind with only connector-specific
        entries (e.g. `scrape`, whose two connectors have wildly different
        cost profiles) intentionally has no `"*"` catch-all, so an
        unrecognised connector under it raises in `price_for()` rather than
        silently under-pricing.

        Every value is env-overridable: `RESEARCH_PRICE_CENTS_<KIND>` for the
        `"*"` entry, `RESEARCH_PRICE_CENTS_<KIND>_<CONNECTOR>` for a
        connector-specific entry (connector `.`/`-` characters uppercased and
        underscored for the env var name, e.g. `ad_library.scrapecreators`
        -> `RESEARCH_PRICE_CENTS_SCRAPE_AD_LIBRARY_SCRAPECREATORS`).

        Defaults (documented, not measured -- re-derive if the underlying
        call site changes):
          - collect/verify/synthesize: 25/10/75 cents. Placeholder worst-case
            ceilings for the not-yet-built connectors (Task 1.6+); tighten
            once each handler's actual model/max_tokens is known.
          - scrape/ad_library.scrapecreators: 80 cents, derived from the
            EXISTING `worker.analyze.analyze()` call: up to `max_images`
            (default 8) images at Sonnet 5's high-resolution worst case
            (~4784 tokens/image) + a generous ~25k-token bound on the
            serialized `distinct_ads` JSON (up to `analysis_cap`=30 rows) and
            system-prompt/angle-registry overhead (~1.5k tokens) for input,
            and the call site's own hardcoded `max_tokens=32000` (thinking +
            output combined -- analyze.py's own ceiling) for output. At
            Claude Sonnet 5's standard (non-introductory) $3/$15 per-MTok
            rate -- deliberately NOT the temporary $2/$10 intro rate, so this
            card stays a valid ceiling after the 2026-08-31 intro window
            closes -- that prices to ~$0.674; rounded up to 80 cents for
            margin.
          - scrape/web.fetch: 0 cents -- a model-free fetch (no paid LLM
            call), per R3/plan Task 1.6.
        """
        defaults: dict = {
            "collect": {"*": 25},
            "verify": {"*": 10},
            "synthesize": {"*": 75},
            "scrape": {
                "ad_library.scrapecreators": 80,
                "web.fetch": 0,
            },
        }
        cards: dict = {}
        for kind, connectors in defaults.items():
            cards[kind] = {}
            for connector, default_cents in connectors.items():
                if connector == "*":
                    env_name = f"RESEARCH_PRICE_CENTS_{kind.upper()}"
                else:
                    suffix = connector.upper().replace(".", "_").replace("-", "_")
                    env_name = f"RESEARCH_PRICE_CENTS_{kind.upper()}_{suffix}"
                cards[kind][connector] = int(os.getenv(env_name, str(default_cents)))
        return cards

    def price_for(self, job_kind: str, connector: Optional[str] = None) -> int:
        """Worst-case cents for one paid call of (job_kind, connector) -- the
        ceiling passed as `rs_reserve_spend`'s `p_est_cents`.

        Looks up an exact (job_kind, connector) entry first; falls back to
        the job_kind's `"*"` wildcard when no connector-specific entry
        exists (or `connector` is None). Raises `KeyError` when neither is
        configured -- fail closed rather than silently reserving 0 cents for
        an unpriced call, which would let a paid call through with no real
        ceiling at all.
        """
        kind_cards = self.price_cards.get(job_kind)
        if kind_cards is None:
            raise KeyError(f"no price card configured for job_kind={job_kind!r}")
        if connector is not None and connector in kind_cards:
            return kind_cards[connector]
        if "*" in kind_cards:
            return kind_cards["*"]
        raise KeyError(
            f"no price card configured for job_kind={job_kind!r} connector={connector!r} "
            f"(known connectors for this job_kind: {sorted(kind_cards)})"
        )


settings = Settings()
