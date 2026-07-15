import os

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


settings = Settings()
