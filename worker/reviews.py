"""Deterministic review-widget text extractor for site captures (P3 EXTRACTION of the
deep-research plan).

`extract_review_text(raw_html) -> str` parses raw captured HTML (stdlib `html.parser`, the
same streaming, O(n), no-backtracking approach `worker.canonical.canonical_text` already
uses) and returns ONLY the rendered text found INSIDE known review-widget containers --
never the rest of the page. This is the customer-voice source-of-truth boundary (S2-5
structural provenance): a VoC quote may only ever be minted against text this function
returned, never against the whole-page canonical text, because a page's own marketing copy
sitting right next to a review widget must never be mistaken for something a customer
actually said.

Detected widgets (by class/id substring match on each class token / the id attribute,
case-insensitive):
  - Judge.me    -- class token starting with "jdgm-rev", or id starting with "judgeme"
  - Loox        -- class token starting with "loox-review"
  - Okendo      -- class token starting with "oke", or any attribute name starting with
                   "data-oke-" (Okendo's custom data-attribute convention)
  - Yotpo       -- class token starting with "yotpo", or exactly "content-review"
  - Stamped     -- class token starting with "stamped-review"
  - Reviews.io  -- class token starting with "reviewsio" or "ruk_"

Deliberately narrow: matching is done PER CLASS TOKEN (the class attribute split on
whitespace, per the HTML spec), not as a substring of the whole attribute string -- so e.g.
a class like "unlocked" can never false-positive-match Okendo's short "oke" marker the way a
raw whole-string substring search would.

Even INSIDE a matched review container, text belonging to a script/style/template/
noscript element is never returned: those tags' content is never rendered text a person
reads (the same `_STRIP_BLOCK_TAGS` reasoning `worker.canonical.canonical_text` documents
for the whole page), so a widget's markup that happens to carry an injected/brand
`<script>` or `<style>` payload can never leak into what this module treats as genuine
customer voice.

Pure function, stdlib only, no new dependency. Empty/None-ish input, or input containing no
matching widget, returns "" (empty string) -- never raises, never guesses.
"""
import re
from html.parser import HTMLParser

_WHITESPACE_RE = re.compile(r"\s+")

# Tags whose entire CONTENT is never rendered text a human reads -- mirrors
# `worker.canonical._STRIP_BLOCK_TAGS` exactly. Suppressed unconditionally, even while
# inside a matched review-widget container: a widget's own script/style/template/noscript
# child is not customer voice just because it sits inside the widget's markup.
_STRIP_BLOCK_TAGS = {"script", "style", "template", "noscript"}

# (label -> {"class_prefixes": [...], "class_exact": [...], "id_prefixes": [...]}) --
# every list is optional; a spec with no "class_exact" simply never matches on it.
_WIDGET_SPECS: dict = {
    "judgeme": {"class_prefixes": ["jdgm-rev"], "id_prefixes": ["judgeme"]},
    "loox": {"class_prefixes": ["loox-review"]},
    "okendo": {"class_prefixes": ["oke"]},
    "yotpo": {"class_prefixes": ["yotpo"], "class_exact": ["content-review"]},
    "stamped": {"class_prefixes": ["stamped-review"]},
    "reviewsio": {"class_prefixes": ["reviewsio", "ruk_"]},
}

_OKENDO_DATA_ATTR_PREFIX = "data-oke-"


def _matches_widget(attrs: list) -> bool:
    """True iff this start tag's attributes identify it as a known review-widget
    container: a class token or id matching one of `_WIDGET_SPECS`'s prefixes/exact
    values, or an Okendo `data-oke-*` attribute present at all."""
    attr_dict: dict = {}
    for name, value in attrs:
        if name:
            attr_dict[name.lower()] = value or ""

    for name in attr_dict:
        if name.startswith(_OKENDO_DATA_ATTR_PREFIX):
            return True

    class_tokens = [t.lower() for t in attr_dict.get("class", "").split()]
    id_value = attr_dict.get("id", "").lower()

    for spec in _WIDGET_SPECS.values():
        for prefix in spec.get("class_prefixes", ()):
            if any(tok.startswith(prefix) for tok in class_tokens):
                return True
        for exact in spec.get("class_exact", ()):
            if exact in class_tokens:
                return True
        for prefix in spec.get("id_prefixes", ()):
            if id_value.startswith(prefix):
                return True
    return False


class _ReviewTextParser(HTMLParser):
    """Streaming HTML-to-text reducer, scoped to review-widget containers only.

    Maintains a real tag stack (`_stack`, one frame per currently-open tag: `{"tag":
    name, "is_widget": bool}`) rather than canonical.py's simple depth counter, because
    unlike script/style (always the same tag name, never meaningfully nested), a review
    widget can be ANY tag and different widget containers can appear side-by-side or
    nested at arbitrary depth -- properly pairing start/end tags by name is required to
    know exactly when a given widget's content ends.

    `_widget_depth` (the count of currently-open frames with `is_widget=True`) gates
    `handle_data`: text is collected only while `_widget_depth > 0`, i.e. while at least
    one ancestor (or the current tag itself) matched a widget pattern -- AND
    `_suppress_depth` (the count of currently-open frames whose tag is a
    script/style/template/noscript strip-block, tracked the same way) is 0. A widget's own
    injected `<script>`/`<style>` child is suppressed even though it is nested inside a
    `_widget_depth > 0` context -- see the module docstring.

    `handle_endtag` is lenient like a real browser: it searches the stack from the top
    for the nearest frame with a matching tag name and implicitly closes everything
    above it too (handles malformed/unclosed nested markup the same way canonical.py's
    simpler counter tolerates malformed script/style nesting). A stray end tag with no
    matching open frame is a no-op.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._stack: list[dict] = []
        self._widget_depth = 0
        self._suppress_depth = 0

    def handle_starttag(self, tag, attrs):
        is_widget = _matches_widget(attrs)
        is_suppress = tag in _STRIP_BLOCK_TAGS
        self._stack.append({"tag": tag, "is_widget": is_widget, "is_suppress": is_suppress})
        if is_widget:
            self._widget_depth += 1
        if is_suppress:
            self._suppress_depth += 1
        self._parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        # Self-closing (e.g. <br/>, <img/>) -- never itself holds text content; a
        # matching self-closing tag can't open an unclosed context either, so this is
        # a boundary space only, never pushed onto the stack.
        self._parts.append(" ")

    def handle_endtag(self, tag):
        self._parts.append(" ")
        idx = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                idx = i
                break
        if idx is None:
            return  # stray/unmatched end tag -- ignore, nothing to close
        removed = self._stack[idx:]
        del self._stack[idx:]
        for frame in removed:
            if frame["is_widget"]:
                self._widget_depth -= 1
            if frame["is_suppress"]:
                self._suppress_depth -= 1

    def handle_data(self, data):
        if self._widget_depth > 0 and self._suppress_depth == 0:
            self._parts.append(data)

    def handle_comment(self, data):
        # A comment's text never contributes; still a node boundary (mirrors
        # canonical.py's own handle_comment reasoning) so two adjacent widget text runs
        # separated only by a comment don't fuse into one word.
        self._parts.append(" ")

    def get_text(self) -> str:
        return "".join(self._parts)


def extract_review_text(raw_html: str) -> str:
    """Extract whitespace-collapsed rendered text from every known review-widget
    container found in `raw_html`. Empty/None-ish input, or input with no matching
    widget anywhere, returns "".

    This is the ONLY function in this module -- deliberately: the review-widget-vs-
    rest-of-page boundary is the single thing this module exists to draw, and every
    caller (the collect job's VoC-quote admission control) must use this text, and only
    this text, as the raw_content passed to `rs_create_voc_quote`.
    """
    if not raw_html:
        return ""

    parser = _ReviewTextParser()
    parser.feed(raw_html)
    parser.close()
    text = parser.get_text()
    return _WHITESPACE_RE.sub(" ", text).strip()
