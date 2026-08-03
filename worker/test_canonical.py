"""Tests for worker/canonical.py: the deterministic canonical-text extractor
whose output is what content_sha256 hashes (server-side, by
rs_create_site_capture). Determinism and nonce-invariance are the entire
point of this module -- most tests below exist to prove exactly that.
"""
from worker.canonical import EXTRACTOR_VERSION, canonical_text


def test_extractor_version_is_the_documented_string():
    assert EXTRACTOR_VERSION == "text@v2"


def test_empty_and_none_input_returns_empty_string():
    assert canonical_text("") == ""
    assert canonical_text(None) == ""


def test_determinism_same_input_same_output():
    html_src = "<html><body><p>Hello  world</p>\n\n<p>Second   line</p></body></html>"
    assert canonical_text(html_src) == canonical_text(html_src)


def test_strips_tags_and_keeps_text():
    html_src = "<div><p>Hello</p><span>world</span></div>"
    assert canonical_text(html_src) == "Hello world"


def test_decodes_html_entities():
    html_src = "<p>Salt &amp; Pepper &#39;quoted&#39;</p>"
    assert canonical_text(html_src) == "Salt & Pepper 'quoted'"


def test_collapses_whitespace_runs_and_trims():
    html_src = "<p>  Hello \n\n\t  world  </p>"
    assert canonical_text(html_src) == "Hello world"


def test_strips_script_block_content_entirely():
    html_src = "<p>Before</p><script>var x = 1; document.write('hidden');</script><p>After</p>"
    text = canonical_text(html_src)
    assert "var x" not in text
    assert "hidden" not in text
    assert text == "Before After"


def test_strips_style_block_content_entirely():
    html_src = "<style>.a { color: red; }</style><p>Visible</p>"
    text = canonical_text(html_src)
    assert "color" not in text
    assert text == "Visible"


def test_strips_template_and_noscript_blocks():
    html_src = (
        "<template><p>never rendered</p></template>"
        "<noscript>enable js please</noscript>"
        "<p>Real content</p>"
    )
    text = canonical_text(html_src)
    assert "never rendered" not in text
    assert "enable js" not in text
    assert text == "Real content"


def test_strips_html_comments():
    html_src = "<p>Before</p><!-- build:abcdef123 nonce=xyz --><p>After</p>"
    text = canonical_text(html_src)
    assert "build" not in text
    assert "nonce" not in text
    assert text == "Before After"


def test_adjacent_inline_tags_do_not_fuse_words():
    html_src = "<td>alpha</td><td>beta</td>"
    text = canonical_text(html_src)
    assert text == "alpha beta"
    assert "alphabeta" not in text


def test_script_nonce_attribute_does_not_affect_output():
    """A CSP nonce on the <script> TAG itself (not its content) must not
    leak into or affect the canonical text -- the whole block is dropped
    regardless of its attributes."""
    a = canonical_text('<script nonce="aaa111">console.log(1)</script><p>Same</p>')
    b = canonical_text('<script nonce="bbb222">console.log(2)</script><p>Same</p>')
    assert a == b == "Same"


# ---------------------------------------------------------------------------
# Nonce-invariance: the whole reason content_sha256 hashes THIS function's
# output rather than raw HTML. Two fetches of "the same page" that only
# differ in a per-request nonce/CSRF token/build id must canonicalize
# IDENTICALLY.
# ---------------------------------------------------------------------------

def test_nonce_invariant_hidden_csrf_input():
    page_a = '<form><input type="hidden" name="csrf" value="nonce-aaa111"></form><p>Content</p>'
    page_b = '<form><input type="hidden" name="csrf" value="nonce-bbb222"></form><p>Content</p>'
    assert canonical_text(page_a) == canonical_text(page_b) == "Content"


def test_nonce_invariant_meta_tag_token():
    page_a = '<meta name="csrf-token" content="tok-111"><p>Content</p>'
    page_b = '<meta name="csrf-token" content="tok-999"><p>Content</p>'
    assert canonical_text(page_a) == canonical_text(page_b) == "Content"


def test_nonce_invariant_inline_script_with_different_nonce_values():
    page_a = '<p>Content</p><script>var csrf = "111aaa";</script>'
    page_b = '<p>Content</p><script>var csrf = "999zzz";</script>'
    assert canonical_text(page_a) == canonical_text(page_b) == "Content"


def test_nonce_invariant_html_comment_build_id():
    page_a = "<!-- build-id: 20260803-aaa --><p>Content</p>"
    page_b = "<!-- build-id: 20260901-zzz --><p>Content</p>"
    assert canonical_text(page_a) == canonical_text(page_b) == "Content"


def test_nonce_invariant_whitespace_only_formatting_differences():
    """Different source whitespace/indentation for the SAME visible content
    (e.g. a minifier setting change) must not change the canonical hash."""
    page_a = "<div>\n  <p>Content</p>\n</div>"
    page_b = "<div><p>Content</p></div>"
    assert canonical_text(page_a) == canonical_text(page_b) == "Content"


def test_content_change_still_produces_different_output():
    """Sanity counterpart to the nonce-invariance tests above: a REAL content
    change must still produce different canonical text (this module must not
    over-normalize into a constant)."""
    page_a = "<p>Original content</p>"
    page_b = "<p>Materially different content</p>"
    assert canonical_text(page_a) != canonical_text(page_b)


# ---------------------------------------------------------------------------
# FIX 2: the `re`-based extractor ("text@v1") could exhibit catastrophic
# backtracking / quadratic blowup on adversarial input (e.g. thousands of
# unterminated `<script`/`<!--` sequences). The `html.parser.HTMLParser`
# rewrite ("text@v2") is a real, linear-time streaming tokenizer with no
# backtracking -- this must return near-instantly on exactly that kind of
# input, not hang or degrade quadratically.
# ---------------------------------------------------------------------------

def test_pathological_unterminated_script_tags_returns_fast():
    """~800 KB of nothing but unterminated `<script>` opens (no closing tag
    anywhere) must canonicalize in well under a second, not hang or exhibit
    quadratic blowup."""
    import time

    payload = "<script>" * 100_000  # ~800 KB, zero closing tags
    start = time.monotonic()
    result = canonical_text(payload)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"canonical_text took {elapsed:.3f}s on pathological <script> input"
    assert isinstance(result, str)


def test_pathological_unterminated_comment_tags_returns_fast():
    """~400 KB of nothing but unterminated `<!--` opens (no closing `-->`
    anywhere) must canonicalize in well under a second."""
    import time

    payload = "<!--" * 100_000  # ~400 KB, zero closing comment markers
    start = time.monotonic()
    result = canonical_text(payload)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"canonical_text took {elapsed:.3f}s on pathological <!-- input"
    assert isinstance(result, str)


def test_pathological_mixed_input_with_real_content_returns_fast_and_correct():
    """A larger (~1.2 MB) mixed pathological payload -- real, well-formed
    content followed by a pile of unterminated `<script>`/`<!--` sequences --
    must still return fast AND still extract the real content correctly.

    (The real content is placed BEFORE the pathological noise, not after:
    an unterminated `<script>` is a genuine HTML5 CDATA element with no
    closing tag anywhere in the document, so -- correctly, not a bug --
    EVERYTHING after its opening tag is consumed as script content,
    including any "real" markup that follows it. That is genuinely correct
    parser behavior, not something this extractor should try to work
    around; placing the real content first is what actually exercises "the
    pathological noise doesn't corrupt genuine content it doesn't swallow
    by construction".)"""
    import time

    payload = "<p>hello world</p>" + ("<script>" * 50_000) + ("<!--" * 50_000)
    start = time.monotonic()
    result = canonical_text(payload)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"canonical_text took {elapsed:.3f}s on mixed pathological input"
    assert "hello world" in result
