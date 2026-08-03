"""Deterministic canonical-text extractor for site captures (web.fetch
connector, Phase 2 of the deep-research plan).

`EXTRACTOR_VERSION` is meant to be threaded into a collect job's
idempotency key (`worker.jobs.idem_collect`, which already takes an
`extractor_version` argument) -- bump it whenever a change to
`canonical_text` would produce different output for the SAME html, so a
downstream collect job keys correctly off "this exact extraction logic ran
over this exact capture" rather than silently reusing a stale key.
`EXTRACTOR_VERSION` is "text@v2" as of the `html.parser` rewrite below (was
"text@v1" for the original `re`-based implementation) -- the two extractors
are not guaranteed byte-identical on every input, so anything keying off the
old version must re-derive.

Determinism is the entire point of this module: `research_site_captures.
content_sha256` is computed server-side (by the `rs_create_site_capture` RPC)
over THIS function's output, not the raw HTML -- so two fetches of a page
whose only difference is a per-request nonce, CSRF token, cache-buster
query-string echoed into a hidden input, or a build timestamp in an HTML
comment must still hash IDENTICALLY, or every single fetch would mint a
spurious "content changed" capture even though nothing a human reader would
call "the page" actually changed. `canonical_text` achieves this by keeping
ONLY rendered text: it strips every element whose content is never text a
person reads (script/style/template/noscript), strips comments (a common
home for build ids and nonces), drops every remaining tag, decodes entities,
and collapses whitespace -- so attribute-level and non-visible noise never
reaches the hash at all.

Pure function, stdlib only (`html.parser.HTMLParser` + `re` for the final
whitespace collapse) -- no new dependency, no external HTML parser.
`HTMLParser` is a real (if lenient) streaming HTML tokenizer: O(n) in input
length, with no backtracking -- unlike the original `re`-based
implementation (kept through "text@v1"), it cannot exhibit catastrophic
backtracking / ReDoS on adversarial input (e.g. a page consisting of
thousands of unterminated `<script`/`<!--` sequences). It is still not a
spec-perfect HTML5 parser (e.g. `<template>`/`<noscript>` content is parsed
as ordinary child nodes rather than given HTML5's special content model),
but for the static-HTML pages this connector targets it is simple,
dependency-free, and -- the property that actually matters here --
perfectly deterministic for a given byte-for-byte input, and safe (linear
time, bounded memory) on ANY input including hostile ones.
"""
import re
from html.parser import HTMLParser

EXTRACTOR_VERSION = "text@v2"

# Tags whose entire CONTENT is never rendered text a human reads. While a
# tag from this set is open, every `handle_data` call is dropped -- so
# JS/CSS source text (which can itself contain "<"/">"-shaped noise, or a
# nonce) never leaks into the canonical text.
_STRIP_BLOCK_TAGS = {"script", "style", "template", "noscript"}

_WHITESPACE_RE = re.compile(r"\s+")


class _CanonicalTextParser(HTMLParser):
    """Streaming (O(n), no backtracking) HTML-to-text reducer.

    - `convert_charrefs=True` (the default, kept explicit for clarity):
      `HTMLParser` itself decodes HTML entities in ordinary text before
      `handle_data` is ever called, so there is no separate entity-decode
      step here (unlike the old `re`-based extractor, which had to run
      `html.unescape` as its own pass AFTER stripping tags).
    - `_strip_depth` is a simple counter, not a per-tag stack: incremented on
      the start of ANY `_STRIP_BLOCK_TAGS` tag, decremented (never below
      zero) on the end of any. This is deliberately approximate for
      malformed/interleaved strip-tag nesting (e.g. `<script><template>
      </script></template>`) -- real HTML never does this, and the
      module-level docstring already documents this extractor as not a
      spec-perfect parser. What it buys in exchange is O(1) extra memory
      even under an adversarial input of thousands of unterminated
      `<script>`/`<template>` opens, where a stack would grow unboundedly.
    - while `_strip_depth > 0`, `handle_data` is a no-op: none of that
      content is rendered text.
    - `handle_comment` contributes a boundary space but never the comment's
      own text -- HTML comments (a common home for build ids/nonces) never
      contribute text, but treating them as a plain node boundary (like
      every start/end tag) avoids fusing two text runs that only had a
      comment between them.
    - a single space is appended at EVERY element boundary (start tag, end
      tag, or self-closing tag) -- not inside a stripped block -- so
      adjacent inline elements never fuse into one word (e.g.
      "<td>a</td><td>b</td>" -> "a b", never "ab"). The boundary-space push
      itself is unconditional (even inside a stripped block) so that content
      immediately after a stripped block never fuses with content from
      before it; only actual `handle_data` text is gated on `_strip_depth`.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._strip_depth = 0

    def handle_starttag(self, tag, attrs):
        self._parts.append(" ")
        if tag in _STRIP_BLOCK_TAGS:
            self._strip_depth += 1

    def handle_startendtag(self, tag, attrs):
        # Self-closing (e.g. <br/>, <img/>) -- boundary space only; it can
        # never itself hold strippable content.
        self._parts.append(" ")

    def handle_endtag(self, tag):
        self._parts.append(" ")
        if tag in _STRIP_BLOCK_TAGS and self._strip_depth > 0:
            self._strip_depth -= 1

    def handle_data(self, data):
        if self._strip_depth > 0:
            return
        self._parts.append(data)

    def handle_comment(self, data):
        # A comment's TEXT never contributes (a common home for build
        # ids/nonces), but it still marks a node boundary -- without this
        # space, "Hello<!-- nonce -->World" (a comment directly between two
        # text runs with no element between them) would otherwise fuse into
        # "HelloWorld". Harmless when it fires inside an already-stripped
        # block (template/noscript can contain a real comment node; the
        # extra space is absorbed by the whitespace collapse either way).
        self._parts.append(" ")

    def get_text(self) -> str:
        return "".join(self._parts)


def canonical_text(html_src: str) -> str:
    """Reduce raw HTML to deterministic, whitespace-normalized rendered
    text. Empty/None-ish input returns "". Linear in input length and safe
    on adversarial input -- see `_CanonicalTextParser` and the module
    docstring.

    Order:
      1. Feed the ENTIRE input through `_CanonicalTextParser` in one pass:
         script/style/template/noscript content is dropped, comments are
         dropped, every element boundary becomes a single space, entities
         are decoded as part of normal `HTMLParser` data handling.
      2. Collapse every whitespace run (spaces, tabs, newlines) to a single
         space and trim -- source-formatting/minifier whitespace is exactly
         the kind of layout noise that varies between requests without the
         rendered content changing.
    """
    if not html_src:
        return ""

    parser = _CanonicalTextParser()
    parser.feed(html_src)
    parser.close()
    text = parser.get_text()
    return _WHITESPACE_RE.sub(" ", text).strip()
