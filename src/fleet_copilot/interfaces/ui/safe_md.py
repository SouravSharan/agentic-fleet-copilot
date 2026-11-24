"""Parser-based sanitizer for untrusted model-written and telemetry-derived markdown.

Uses `markdown-it-py` (CommonMark preset) to parse untrusted text into a token tree and
re-emits ONLY a narrow safe subset:
- paragraphs, emphasis (`*...*`), strong (`**...**`), bullet and ordered lists,
  inline code spans, and fenced code blocks.

Everything else is stripped or neutralised:
- `image` tokens: dropped completely (only plain alt text is kept and escaped).
- `link_open`/`link_close` tokens (including inline, reference, and `<autolink>` forms):
  dropped completely (visible inner text is kept and escaped).
- Reference link definitions (`[id]: url`): consumed and dropped by the parser.
- `html_inline` and `html_block` tokens: dropped completely (zero raw HTML survives).
- Plain text tokens: markdown/Streamlit control punctuation escaped (`\\`, `` ` ``, `*`, `_`,
  `[`, `]`, `(`, `)`, `!`, `<`, `>`, `#`, `~`, `|`, `$`, `:`, plus line-leading `-`/`+`/`.`),
  and GFM autolinkable literals (`http(s)://`, `//host`, `www.`, `mailto:`, email addresses)
  wrapped in safe inline code spans so Streamlit's `remark-gfm` cannot linkify them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

# Full CommonMark parser (bounded nesting) used when link/image/reference syntax could exist.
_MD_FULL = MarkdownIt("commonmark", {"maxNesting": 8})
# Fast CommonMark parser with link/image/reference rules disabled when neither `](` nor `]:`
# appears (mathematically impossible for CommonMark inline or reference links/images to form,
# eliminating pathological Python overhead on long unmatched `[` runs).
_MD_NO_LINKS = MarkdownIt("commonmark", {"maxNesting": 8}).disable(["link", "image", "reference"])

# GFM autolink literals that Streamlit's `remark-gfm` would linkify inside text nodes even after
# CommonMark backslash escapes: http(s):// (any case), mailto:, protocol-relative //host, www.,
# and bare email addresses.
_GFM_AUTOLINK_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"(?:https?://|mailto:)[^\s<>()\[\]\"'`]+"
    r"|(?<![A-Za-z0-9:/])//[^\s<>()\[\]\"'`/][^\s<>()\[\]\"'`]*"
    r"|\bwww\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+[^\s<>()\[\]\"'`]*"
    r"|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"
    r")"
)
_TRAILING_URL_PUNCT = ".,;:!?)>]}'\""

# Characters escaped in plain text tokens so neither CommonMark, GFM, nor Streamlit directives
# (`:color[...]`, `:material/...:`, `$math$`) can activate. CommonMark renders all of these
# backslash-escaped ASCII punctuation marks cleanly as their literal characters.
_ESCAPE_CHARS_RE = re.compile(r"([\\`*_\[\]()!<>#~|$:])")
_LINE_LEADING_LIST_RE = re.compile(r"(^|\n)([ \t]*)([-+])(?=[ \t]|$)")
_LINE_LEADING_OL_RE = re.compile(r"(^|\n)([ \t]*\d+)\.(?=[ \t]|$)")
_SAFE_FENCE_INFO_RE = re.compile(r"[^A-Za-z0-9_+-]+")
_BACKTICK_RUN_RE = re.compile(r"`+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_code(text: object) -> str:
    """Sanitize a value intended for interpolation inside markdown backticks `` `...` ``.

    Strips backticks and replaces newlines/tabs/control chars with single spaces so untrusted
    telemetry or model values can never break out of an inline code span.
    """
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""
    cleaned = raw.replace("`", "")
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    return cleaned.strip()


def _emit_code_inline(content: str) -> str:
    """Emit a CommonMark-safe inline code span that cannot be broken by internal backticks."""
    flat = re.sub(r"[\r\n]+", " ", content)
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(flat)), default=0)
    delim = "`" * (longest + 1)
    needs_pad = bool(flat) and (
        flat.startswith("`")
        or flat.endswith("`")
        or (flat.startswith(" ") and flat.endswith(" ") and flat.strip() != "")
    )
    if needs_pad:
        return f"{delim} {flat} {delim}"
    return f"{delim}{flat}{delim}"


def _emit_code_block(content: str, info: str = "") -> str:
    """Emit a CommonMark-safe fenced code block using a fence longer than internal backticks."""
    body = content.rstrip("\n")
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(body)), default=0)
    fence = "`" * max(3, longest + 1)
    first_word = (info or "").strip().split()[0] if (info or "").strip() else ""
    lang = _SAFE_FENCE_INFO_RE.sub("", first_word)[:24]
    return f"{fence}{lang}\n{body}\n{fence}"


def _escape_plain_chunk(chunk: str) -> str:
    if not chunk:
        return ""
    out = _ESCAPE_CHARS_RE.sub(r"\\\1", chunk)
    out = _LINE_LEADING_LIST_RE.sub(r"\1\2\\\3", out)
    out = _LINE_LEADING_OL_RE.sub(r"\1\2\\.", out)
    return out


def _emit_plain_text(text: str) -> str:
    """Escape plain text while wrapping GFM-autolinkable literals in safe inline code spans."""
    if not text:
        return ""
    parts: list[str] = []
    cursor = 0
    for match in _GFM_AUTOLINK_RE.finditer(text):
        start, end = match.span()
        if start > cursor:
            parts.append(_escape_plain_chunk(text[cursor:start]))
        token = match.group(0)
        tail = ""
        while token and token[-1] in _TRAILING_URL_PUNCT:
            tail = token[-1] + tail
            token = token[:-1]
        if token:
            parts.append(_emit_code_inline(token))
            if tail:
                parts.append(_escape_plain_chunk(tail))
        else:
            parts.append(_escape_plain_chunk(match.group(0)))
        cursor = end
    if cursor < len(text):
        parts.append(_escape_plain_chunk(text[cursor:]))
    return "".join(parts)


def _render_inline(children: Sequence[Token] | None) -> str:
    if not children:
        return ""
    out: list[str] = []
    for tok in children:
        ttype = tok.type
        if ttype == "text":
            out.append(_emit_plain_text(tok.content))
        elif ttype == "code_inline":
            out.append(_emit_code_inline(tok.content))
        elif ttype == "strong_open":
            out.append("**")
        elif ttype == "strong_close":
            out.append("**")
        elif ttype == "em_open":
            out.append("*")
        elif ttype == "em_close":
            out.append("*")
        elif ttype == "softbreak":
            out.append("\n")
        elif ttype == "hardbreak":
            out.append("  \n")
        elif ttype == "image":
            # Drop image syntax/URL; keep only sanitized alt text from children (or content).
            if tok.children:
                out.append(_render_inline(tok.children))
            elif tok.content:
                out.append(_emit_plain_text(tok.content))
        elif ttype in ("link_open", "link_close", "html_inline"):
            # Drop link wrappers (inner text children remain) and drop raw inline HTML tags.
            continue
        elif tok.content:
            out.append(_emit_plain_text(tok.content))
    return "".join(out)


def _indent_lines(text: str, prefix: str) -> str:
    lines = text.split("\n")
    return "\n".join((prefix + ln if ln else ln) for ln in lines)


def _render_blocks(tokens: Sequence[Token]) -> str:
    """Walk block tokens produced by `markdown-it-py` and emit safe markdown blocks."""
    # Track active list stack: list of (kind: "bullet"|"ordered", next_index: int)
    list_stack: list[list[Any]] = []
    # Blocks accumulated per scope; index 0 is root, deeper indices collect one list item's blocks.
    scope_blocks: list[list[str]] = [[]]
    item_meta_stack: list[tuple[str, int]] = []

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        ttype = tok.type

        if ttype == "bullet_list_open":
            list_stack.append(["bullet", 0])
            scope_blocks.append([])
        elif ttype == "ordered_list_open":
            start_attr = tok.attrGet("start")
            start_num = int(start_attr) if start_attr is not None else 1
            list_stack.append(["ordered", start_num])
            scope_blocks.append([])
        elif ttype in ("bullet_list_close", "ordered_list_close"):
            items_rendered = scope_blocks.pop()
            if list_stack:
                list_stack.pop()
            joined = "\n".join(items_rendered)
            if joined:
                scope_blocks[-1].append(joined)
        elif ttype == "list_item_open":
            if list_stack:
                kind, idx = list_stack[-1]
                if kind == "ordered":
                    item_meta_stack.append(("ordered", int(idx)))
                    list_stack[-1][1] = int(idx) + 1
                else:
                    item_meta_stack.append(("bullet", 0))
            else:
                item_meta_stack.append(("bullet", 0))
            scope_blocks.append([])
        elif ttype == "list_item_close":
            item_blocks = scope_blocks.pop()
            kind, num = item_meta_stack.pop() if item_meta_stack else ("bullet", 0)
            marker = f"{num}. " if kind == "ordered" else "- "
            cont_indent = " " * len(marker)
            non_empty = [b for b in item_blocks if b]
            if non_empty:
                first_lines = non_empty[0].split("\n")
                rendered_first = marker + first_lines[0]
                if len(first_lines) > 1:
                    rendered_first += "\n" + _indent_lines("\n".join(first_lines[1:]), cont_indent)
                rest = [_indent_lines(b, cont_indent) for b in non_empty[1:]]
                item_str = "\n\n".join([rendered_first, *rest]) if rest else rendered_first
                scope_blocks[-1].append(item_str)
        elif ttype == "inline":
            rendered = _render_inline(tok.children)
            if rendered:
                scope_blocks[-1].append(rendered)
        elif ttype in ("fence", "code_block"):
            scope_blocks[-1].append(_emit_code_block(tok.content, tok.info))
        elif ttype in (
            "html_block",
            "hr",
            "paragraph_open",
            "paragraph_close",
            "heading_open",
            "heading_close",
            "blockquote_open",
            "blockquote_close",
        ):
            # Drop raw HTML blocks and horizontal rules; paragraphs/headings/blockquotes emit
            # their child `inline` tokens cleanly as plain text blocks.
            pass
        i += 1

    root = [b for b in scope_blocks[0] if b]
    return "\n\n".join(root)


def safe_md(text: object) -> str:
    """Return a parser-sanitized markdown string safe for Streamlit rendering.

    Drops images, links (preserving visible link text), autolinks, raw HTML, and reference
    definitions; preserves emphasis, bullet/ordered lists, inline code, and fenced code blocks;
    escapes markdown/Streamlit control characters in plain text and wraps GFM autolink literals
    in inline code spans.
    """
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""
    # Normalize CRLF/CR to LF and strip NUL/control bytes (except tab/newline)
    cleaned = _CONTROL_RE.sub("", raw.replace("\r\n", "\n").replace("\r", "\n"))
    if not cleaned:
        return ""

    parser = _MD_FULL if ("](" in cleaned or "]:" in cleaned) else _MD_NO_LINKS
    tokens = parser.parse(cleaned)
    return _render_blocks(tokens)
