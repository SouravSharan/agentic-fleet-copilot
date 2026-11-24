"""Sanitize untrusted model-written and telemetry-derived text before Streamlit markdown rendering.

Rules applied outside code spans (`...` and ```...```):
1. Strip markdown images `![alt](url)` -> `alt` (prevents external image beacon fetches).
2. Strip markdown links `[text](url)` -> `text` (leaves bare citation handles `[E2]`, `[C1]`, `[Q1]`
   intact because they have no trailing `(url)`).
3. Wrap bare `http://` / `https://` URLs in inline backticks so Streamlit does not autolink them.
4. Escape `<` and `>` to `&lt;` / `&gt;` so raw HTML tags cannot render.
"""

from __future__ import annotations

import re

# Match fenced code blocks or inline code spans (preserving them verbatim)
_CODE_SEGMENT_RE = re.compile(r"(```[\s\S]*?```|(`+)(?!`)(?:(?!\2)[\s\S])+?\2)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^()\r\n]|\([^()\r\n]*\))*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^()\r\n]|\([^()\r\n]*\))*\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
_TRAILING_PUNCT = ".,;:!?"


def _wrap_bare_url(match: re.Match[str]) -> str:
    url = match.group(0)
    tail = ""
    while url and url[-1] in _TRAILING_PUNCT:
        tail = url[-1] + tail
        url = url[:-1]
    if not url:
        return match.group(0)
    return f"`{url}`{tail}"


def _sanitize_plain_segment(segment: str) -> str:
    text = segment
    # Iteratively strip nested image/link wrappers such as [![img](u1)](u2)
    for _ in range(4):
        prev = text
        text = _MD_IMAGE_RE.sub(r"\1", text)
        text = _MD_LINK_RE.sub(r"\1", text)
        if text == prev:
            break
    # Neutralize bare http(s):// autolinks before escaping angle brackets
    text = _BARE_URL_RE.sub(_wrap_bare_url, text)
    # Escape raw HTML angle brackets outside code spans
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text


def safe_md(text: object) -> str:
    """Return a markdown-safe representation of untrusted model or telemetry text.

    Leaves inline and fenced backtick code spans verbatim and preserves citation handles
    such as `[E2]`, `[C1]`, and `[Q1]`.
    """
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""

    out: list[str] = []
    cursor = 0
    for match in _CODE_SEGMENT_RE.finditer(raw):
        start, end = match.span()
        if start > cursor:
            out.append(_sanitize_plain_segment(raw[cursor:start]))
        # Preserve code spans untouched
        out.append(match.group(0))
        cursor = end
    if cursor < len(raw):
        out.append(_sanitize_plain_segment(raw[cursor:]))
    return "".join(out)
