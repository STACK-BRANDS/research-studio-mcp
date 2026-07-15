"""Ingestion: resolve a competitor's Meta platform id, pull their ads, dedup by
ad_id, and fetch a capped set of creative images with an SSRF-safe downloader.
"""

import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from src.services.scrapecreators_service import get_ads, get_platform_id
from worker.config import settings

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_SCHEMES = {"http", "https"}


def _norm(s: str) -> str:
    """Collapse to lowercase alphanumerics — so 'Sophie Olivia' matches
    'Sophie & Olivia' and 'Secret Coco' matches 'SecretCoco.com'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_platform_id(brand: str) -> str:
    """Resolve a brand name to its Meta platform id via ScrapeCreators search.
    Tries, in order: exact, case-insensitive exact, normalized-exact (ignoring
    '&'/spaces/punctuation), then a unique normalized-prefix match. Raises with
    the candidate list only when no confident match exists — never guesses among
    genuinely ambiguous results. Pass the exact page name to force a specific one.
    """
    options = get_platform_id(brand)
    if not options:
        raise ValueError(f"No platform id found for brand '{brand}'")

    # 1. exact / case-insensitive exact
    if brand in options:
        return options[brand]
    for name, page_id in options.items():
        if name.lower() == brand.lower():
            return page_id

    nb = _norm(brand)

    # 2. normalized-exact (e.g. "Sophie Olivia" -> "Sophie & Olivia").
    #    If several, prefer the shortest name (the base page over "… Intimates"/"… II").
    norm_exact = sorted(
        [(name, pid) for name, pid in options.items() if _norm(name) == nb],
        key=lambda x: len(x[0]),
    )
    if norm_exact:
        logger.info("resolve_platform_id: normalized match '%s' for '%s'", norm_exact[0][0], brand)
        return norm_exact[0][1]

    # 3. unique normalized-prefix (e.g. "Secret Coco" -> "SecretCoco.com").
    prefix = [(name, pid) for name, pid in options.items() if _norm(name).startswith(nb)]
    if len(prefix) == 1:
        logger.info("resolve_platform_id: unique prefix match '%s' for '%s'", prefix[0][0], brand)
        return prefix[0][1]

    raise ValueError(
        f"No confident match for '{brand}'. Candidates: {list(options.keys())}. "
        f"Re-run with the exact page name as the brand argument."
    )


def pull_ads(platform_id: str) -> list[dict]:
    """Pull raw ads for a platform id. `trim=False` keeps the extra fields
    (impressions, spend, effective_status, ...) the analysis prompt wants.
    """
    return get_ads(platform_id, limit=settings.ad_limit, trim=False)


def dedup(ads: list[dict]) -> list[dict]:
    """Collapse ads by ad_id, keeping the first occurrence and preserving
    input order.
    """
    seen: set = set()
    out: list[dict] = []
    for ad in ads:
        ad_id = ad.get("ad_id")
        if ad_id in seen:
            continue
        seen.add(ad_id)
        out.append(ad)
    return out


_START_FIELDS = ("start_date", "startDate", "ad_delivery_start_time", "start_time")
_END_FIELDS = ("end_date", "endDate", "ad_delivery_stop_time", "end_time")


def _parse_dt(v):
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        s = str(v).strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None


def _first(ad: dict, fields):
    for f in fields:
        if ad.get(f) not in (None, ""):
            return ad.get(f)
    return None


def _days_active(ad: dict):
    start = _parse_dt(_first(ad, _START_FIELDS))
    if start is None:
        return None
    end = _parse_dt(_first(ad, _END_FIELDS)) or datetime.now(timezone.utc)
    return max(0, (end - start).days)


def _is_active(ad: dict) -> bool:
    end = _parse_dt(_first(ad, _END_FIELDS))
    if end is None:
        return True  # no stop time known → treat as active (Ad Library default)
    return end >= datetime.now(timezone.utc)


def select_for_analysis(ads: list[dict], cap: int | None = None) -> list[dict]:
    """Scope-aware sample for the expensive analysis: prefer active ads, then a
    recency + longevity stratified sample (freshest N + longest-running N) capped
    at `cap`. Date-aware when the ad objects carry parseable start/end dates;
    falls back to input order (Ad Library returns newest-first) otherwise. The raw
    snapshot keeps EVERYTHING — this only bounds what Claude analyzes.
    """
    cap = cap or settings.analysis_cap
    active = [a for a in ads if _is_active(a)] or ads
    if len(active) <= cap:
        return active

    dated = [(a, _days_active(a)) for a in active]
    if sum(1 for _, d in dated if d is not None) >= cap:
        # ascending days_active → smallest = newest, largest = longest-running
        by_days = sorted(dated, key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0))
        half = cap // 2
        freshest = [a for a, _ in by_days[:half]]
        longest = [a for a, _ in reversed(by_days)][: cap - half]
        picked, seen = [], set()
        for a in freshest + longest:
            k = a.get("ad_id")
            if k in seen:
                continue
            seen.add(k)
            picked.append(a)
        return picked[:cap]

    # order-based fallback: front (fresh) + back (long-running)
    half = cap // 2
    return active[:half] + active[-(cap - half):]


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_url_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    return bool(infos) and all(_is_public_ip(info[4][0]) for info in infos)


def _safe_download(url: str, cap_bytes: int = MAX_IMAGE_BYTES):
    """Download `url` with an SSRF boundary: scheme allowlist, DNS-resolved
    private/loopback/link-local/reserved/multicast/unspecified IP rejection,
    at most one manually-validated redirect hop, image/* content-type only,
    and a byte cap enforced both via Content-Length and while streaming.
    Returns (raw_bytes, content_type) or None if any check fails.
    """
    if not _validate_url_host(url):
        return None
    try:
        resp = requests.get(url, timeout=30, stream=True, allow_redirects=False)
    except requests.RequestException:
        return None

    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        if not location or not _validate_url_host(location):
            return None
        try:
            resp = requests.get(location, timeout=30, stream=True, allow_redirects=False)
        except requests.RequestException:
            return None

    if resp.status_code != 200:
        return None

    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        return None

    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > cap_bytes:
                return None
        except ValueError:
            pass

    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > cap_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks), content_type


def fetch_images(distinct_ads: list[dict], cap: int | None = None) -> list[tuple[str, bytes, str]]:
    """Download up to `cap` distinct creatives (images only, videos skipped).
    Any single ad whose image fails the SSRF/size/type checks is skipped —
    this never raises for a bad URL, it logs and continues.
    """
    cap = cap or settings.max_images
    out: list[tuple[str, bytes, str]] = []
    for ad in distinct_ads:
        if len(out) >= cap:
            break
        media_url = ad.get("media_url")
        media_type = ad.get("media_type", "")
        if not media_url or media_type == "VIDEO":
            continue
        result = _safe_download(media_url)
        if result is None:
            logger.warning(
                "skipping media_url for ad_id=%s: SSRF check or download failed",
                ad.get("ad_id"),
            )
            continue
        raw, content_type = result
        out.append((ad.get("ad_id"), raw, content_type))
    return out
