import os

class Settings:
    anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    scrapecreators_api_key = os.environ["SCRAPECREATORS_API_KEY"]
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_service_key = os.environ["SUPABASE_SERVICE_KEY"]  # sb_secret_ (new key)
    model = os.getenv("RESEARCH_MODEL", "claude-sonnet-5")
    max_images = int(os.getenv("RESEARCH_MAX_IMAGES", "8"))
    ad_limit = int(os.getenv("RESEARCH_AD_LIMIT", "25"))

settings = Settings()
