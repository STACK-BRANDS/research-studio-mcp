"""Tests for worker/reviews.py: extract_review_text -- deterministic, stdlib-only
extraction of rendered text from known review-widget containers (Judge.me, Loox,
Okendo, Yotpo, Stamped, Reviews.io), and nothing else on the page.
"""
from worker.reviews import extract_review_text


# ---------------------------------------------------------------------------
# No widget present -- must return "", never guess.
# ---------------------------------------------------------------------------

def test_no_widget_returns_empty_string():
    html = "<html><body><h1>Welcome</h1><p>Buy our lovely lingerie today.</p></body></html>"
    assert extract_review_text(html) == ""


def test_empty_input_returns_empty_string():
    assert extract_review_text("") == ""
    assert extract_review_text(None) == ""


def test_page_with_only_marketing_copy_never_leaks_into_output():
    html = (
        "<body><div class='hero'>Buy 2 get 1 free, today only!</div>"
        "<div class='jdgm-rev-widg'><div class='jdgm-rev__body'>Super comfy, true to size.</div></div>"
        "</body>"
    )
    text = extract_review_text(html)
    assert "comfy" in text
    assert "Buy 2 get 1 free" not in text


# ---------------------------------------------------------------------------
# Judge.me -- class token starting with "jdgm-rev", or id starting with "judgeme".
# ---------------------------------------------------------------------------

def test_judgeme_class_widget_extracted():
    html = "<div class='jdgm-rev-widg'><span class='jdgm-rev__body'>Fits perfectly, will buy again.</span></div>"
    assert "Fits perfectly, will buy again." in extract_review_text(html)


def test_judgeme_id_widget_extracted():
    html = "<div id='judgeme_product_reviews'>Great quality fabric.</div>"
    assert "Great quality fabric." in extract_review_text(html)


# ---------------------------------------------------------------------------
# Loox -- class token starting with "loox-review".
# ---------------------------------------------------------------------------

def test_loox_widget_extracted():
    html = "<div class='loox-reviews-default'><div class='loox-review-content'>So soft and true to size.</div></div>"
    assert "So soft and true to size." in extract_review_text(html)


# ---------------------------------------------------------------------------
# Okendo -- class token starting with "oke", or any data-oke-* attribute.
# ---------------------------------------------------------------------------

def test_okendo_class_widget_extracted():
    html = "<div class='oke-reviews-widget'>Runs a little small, size up.</div>"
    assert "Runs a little small, size up." in extract_review_text(html)


def test_okendo_data_attribute_widget_extracted():
    html = "<div data-oke-reviews-widget='product-123'>Colors are exactly as pictured.</div>"
    assert "Colors are exactly as pictured." in extract_review_text(html)


def test_okendo_short_prefix_does_not_false_positive_on_unrelated_class():
    """FALSE-POSITIVE GUARD: Okendo's "oke" marker matches by class-TOKEN prefix, not
    whole-string substring -- a class like "unlocked" (which contains the substring
    "oke" but does not START a token with it) must never be mistaken for a widget."""
    html = "<div class='unlocked-content'>This page is completely unrelated marketing copy.</div>"
    assert extract_review_text(html) == ""


# ---------------------------------------------------------------------------
# Yotpo -- class token starting with "yotpo", or exactly "content-review".
# ---------------------------------------------------------------------------

def test_yotpo_class_widget_extracted():
    html = "<div class='yotpo-reviews'><span class='yotpo-review-stars'>Absolutely love the lace detail.</span></div>"
    assert "Absolutely love the lace detail." in extract_review_text(html)


def test_yotpo_content_review_exact_class_extracted():
    html = "<div class='content-review'>Bought as a gift, she loved it.</div>"
    assert "Bought as a gift, she loved it." in extract_review_text(html)


# ---------------------------------------------------------------------------
# Stamped -- class token starting with "stamped-review".
# ---------------------------------------------------------------------------

def test_stamped_widget_extracted():
    html = "<div class='stamped-reviews-widget'><p class='stamped-review-content'>Held up great after many washes.</p></div>"
    assert "Held up great after many washes." in extract_review_text(html)


# ---------------------------------------------------------------------------
# Reviews.io -- class token starting with "reviewsio" (case-insensitive) or "ruk_".
# ---------------------------------------------------------------------------

def test_reviewsio_class_widget_extracted():
    html = "<div class='REVIEWSIO-carousel-widget'>Customer service was fantastic too.</div>"
    assert "Customer service was fantastic too." in extract_review_text(html)


def test_reviewsio_ruk_prefix_widget_extracted():
    html = "<div class='ruk_rating_snippet'>Five stars, exceeded expectations.</div>"
    assert "Five stars, exceeded expectations." in extract_review_text(html)


# ---------------------------------------------------------------------------
# Structural behavior: nesting, boundaries, multiple widgets, malformed HTML.
# ---------------------------------------------------------------------------

def test_nested_tags_within_widget_do_not_fuse_adjacent_text():
    html = (
        "<div class='jdgm-rev-widg'>"
        "<span class='jdgm-rev__author'>Amy</span>"
        "<span class='jdgm-rev__body'>Love it</span>"
        "</div>"
    )
    text = extract_review_text(html)
    assert "Amy" in text and "Love it" in text
    assert "AmyLove" not in text  # boundary space between adjacent inline elements


def test_multiple_separate_widgets_all_extracted():
    html = (
        "<div class='jdgm-rev-widg'>Comfortable all day.</div>"
        "<p>Some unrelated page text in between.</p>"
        "<div class='loox-review-item'>Perfect fit for my shape.</div>"
    )
    text = extract_review_text(html)
    assert "Comfortable all day." in text
    assert "Perfect fit for my shape." in text
    assert "unrelated page text" not in text


def test_widget_closes_correctly_when_followed_by_more_page_text():
    html = (
        "<div class='jdgm-rev-widg'>Reviewer text here.</div>"
        "<div class='footer'>Copyright 2026 -- All rights reserved.</div>"
    )
    text = extract_review_text(html)
    assert "Reviewer text here." in text
    assert "Copyright" not in text


def test_self_closing_tag_inside_widget_does_not_crash_or_leak():
    html = "<div class='jdgm-rev-widg'>Great product<br/>would recommend</div>"
    text = extract_review_text(html)
    assert "Great product" in text
    assert "would recommend" in text


def test_malformed_unclosed_widget_tag_does_not_crash():
    """A widget div that is never explicitly closed (falls off the end of input) must
    not raise -- html.parser simply runs out of input; whatever text was inside is
    still captured."""
    html = "<div class='jdgm-rev-widg'>Never closes properly"
    text = extract_review_text(html)
    assert "Never closes properly" in text


def test_stray_end_tag_without_matching_open_is_ignored():
    html = "</div><div class='jdgm-rev-widg'>Still works fine.</div>"
    text = extract_review_text(html)
    assert "Still works fine." in text


def test_whitespace_is_collapsed_and_trimmed():
    html = "<div class='jdgm-rev-widg'>   lots\n\n  of   whitespace   here  </div>"
    assert extract_review_text(html) == "lots of whitespace here"


# ---------------------------------------------------------------------------
# FIX 1: script/style/template/noscript inside a review container must never
# leak into the extracted text -- a widget's own injected/brand payload hidden
# in one of these elements must not be mistaken for genuine customer voice.
# ---------------------------------------------------------------------------

def test_script_inside_review_container_is_suppressed():
    html = (
        "<div class='jdgm-rev-widg'>Genuine review text."
        "<script>evil_inject_brand_copy()</script>"
        "</div>"
    )
    text = extract_review_text(html)
    assert "Genuine review text." in text
    assert "evil_inject_brand_copy" not in text


def test_style_inside_review_container_is_suppressed():
    html = (
        "<div class='jdgm-rev-widg'>Genuine review text."
        "<style>.fake { content: 'INJECTED BRAND COPY'; }</style>"
        "</div>"
    )
    text = extract_review_text(html)
    assert "Genuine review text." in text
    assert "INJECTED BRAND COPY" not in text


def test_template_inside_review_container_is_suppressed():
    html = (
        "<div class='jdgm-rev-widg'>Genuine review text."
        "<template>Buy 2 get 1 free, injected via template!</template>"
        "</div>"
    )
    text = extract_review_text(html)
    assert "Genuine review text." in text
    assert "Buy 2 get 1 free" not in text


def test_noscript_inside_review_container_is_suppressed():
    html = (
        "<div class='jdgm-rev-widg'>Genuine review text."
        "<noscript>Injected brand copy via noscript fallback.</noscript>"
        "</div>"
    )
    text = extract_review_text(html)
    assert "Genuine review text." in text
    assert "Injected brand copy" not in text
