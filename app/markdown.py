"""Server-side markdown → HTML. Raw HTML in notes is escaped; code highlighting happens in the browser."""
import re

from markdown_it import MarkdownIt

from .notes_fs import slugify

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])


def _heading_ids(state):
    seen = {}
    tokens = state.tokens
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and i + 1 < len(tokens):
            base = slugify(tokens[i + 1].content) or "section"
            n = seen.get(base, 0)
            seen[base] = n + 1
            tok.attrSet("id", base if n == 0 else f"{base}-{n}")


_md.core.ruler.push("heading_ids", _heading_ids)

WIKI_RE = re.compile(r"\[\[([A-Za-z]\d{3})\]\]")
# Split rendered HTML so wiki links are never rewritten inside code.
CODE_SPLIT_RE = re.compile(r"(<pre>.*?</pre>|<code>.*?</code>)", re.S)


def render(text: str, known_codes: set[str] | frozenset = frozenset()) -> str:
    html = _md.render(text or "")

    def link(m):
        code = m.group(1).upper()
        if code in known_codes:
            return f'<a class="course-link" href="/courses/{code}">{code}</a>'
        return f'<span class="unknown-course" title="No course {code} in your plan">{code}</span>'

    parts = CODE_SPLIT_RE.split(html)
    return "".join(p if i % 2 else WIKI_RE.sub(link, p) for i, p in enumerate(parts))


def heading_slug(text: str) -> str:
    return slugify(text) or "section"
