"""Unit tests for parser-based `safe_md` and `safe_code` sanitizers."""

from __future__ import annotations

import time

import pytest
from markdown_it import MarkdownIt

from fleet_copilot.interfaces.ui.safe_md import safe_code, safe_md

_VERIFY_MD = MarkdownIt("commonmark")


def _assert_no_active_dangerous_tokens(sanitized: str) -> None:
    """Re-parse sanitized markdown with CommonMark and assert zero images, links, or raw HTML."""
    tokens = _VERIFY_MD.parse(sanitized)
    for tok in tokens:
        assert tok.type not in ("html_block", "html_inline"), f"Raw HTML survived: {tok}"
        for child in tok.children or ():
            assert child.type not in (
                "image",
                "link_open",
                "link_close",
                "html_inline",
            ), f"Active dangerous token {child.type!r} survived in {sanitized!r}"


@pytest.mark.parametrize(
    ("attack", "forbidden_substrings"),
    [
        # Nested brackets in image alt text
        ("![a [b]](//evil/p.png)", ["//evil/p.png", "![", "]("]),
        # Newline inside image destination
        ("![a](\n//evil/p.png)", ["//evil/p.png", "![", "]("]),
        # Reference-style image + reference definition
        ("![a][r]\n\n[r]: //evil/p.png", ["//evil/p.png", "![", "[r]:"]),
        # Reference-style link + reference definition
        ("[click][r]\n\n[r]: https://evil.example/phish", ["https://evil.example/phish", "[r]:"]),
        # Shortcut reference link
        ("[r]\n\n[r]: //evil.example/x", ["//evil.example/x", "[r]:"]),
        # Nested image inside link and image inside image
        ("[![pic](https://evil.example/a.png)](https://evil.example/b)", ["evil.example", "!["]),
        ("![outer ![inner](//e1)](//e2)", ["//e1", "//e2", "!["]),
        # Mismatched backtick runs attempting to swallow an image into fake code
        ("``![a](//e)```", ["//e", "![", "]("]),
        # Unclosed backtick spanning a blank line before an image
        ("`a\n\n![x](//e)`", ["//e", "![", "]("]),
        # Escaped exclamation mark before link
        (r"foo \![not-img](//e)", ["//e", "]("]),
        # Raw HTML inline and block tags
        ("<b>x</b><img src=x onerror=alert(1)>", ["<b>", "<img", "onerror"]),
        ("<script>alert(1)</script>", ["<script", "alert(1)"]),
        ("<a href='https://evil.example'>Re-auth</a>", ["<a ", "href", "evil.example"]),
        # Javascript URI link
        ("Bad [js](javascript:alert(1)) link", ["javascript:", "]("]),
    ],
)
def test_bypass_corpus_strips_active_markup_and_urls(
    attack: str, forbidden_substrings: list[str]
) -> None:
    out = safe_md(attack)
    _assert_no_active_dangerous_tokens(out)
    for bad in forbidden_substrings:
        assert bad not in out, f"Expected {bad!r} absent from {out!r}"


@pytest.mark.parametrize(
    ("raw_url_text", "expected_code_span"),
    [
        ("Visit https://evil.example/x?a=1.", "`https://evil.example/x?a=1`"),
        ("Uppercase HTTPS://EVIL.EXAMPLE/leak", "`HTTPS://EVIL.EXAMPLE/leak`"),
        ("Protocol-relative //evil.example/p.png", "`//evil.example/p.png`"),
        ("GFM autolink www.evil.com/path.", "`www.evil.com/path`"),
        ("Angle autolink <https://evil.example/a>", "`https://evil.example/a`"),
        ("Email mailto:bad@evil.example or bad@evil.example", "`mailto:bad@evil.example`"),
    ],
)
def test_bare_urls_and_gfm_autolinks_wrapped_in_code_spans(
    raw_url_text: str, expected_code_span: str
) -> None:
    out = safe_md(raw_url_text)
    _assert_no_active_dangerous_tokens(out)
    assert expected_code_span in out
    assert "<https://" not in out


def test_preserves_normal_answers_emphasis_lists_code_and_handles() -> None:
    normal = (
        "Three laptops need service: **acme-macbook-1** (`disk_free_pct < 15`) [E1, E2].\n\n"
        "- *Item one* with < 15% free and chart [C1]\n"
        "- Item two from SQL [Q1]\n\n"
        "1. First step\n"
        "2. Second step\n\n"
        "```sql\nSELECT * FROM v WHERE x < 10 AND y > 1\n```"
    )
    out = safe_md(normal)
    _assert_no_active_dangerous_tokens(out)
    # Bold, italic, inline code, fenced code, bullet & numbered lists preserved cleanly
    assert "**acme-macbook-1**" in out
    assert "`disk_free_pct < 15`" in out
    assert "*Item one*" in out
    assert "- *Item one*" in out
    assert "1. First step" in out
    assert "2. Second step" in out
    assert "```sql\nSELECT * FROM v WHERE x < 10 AND y > 1\n```" in out
    # Citation handles stay plain text that CommonMark renders cleanly as [E1, E2], [C1], [Q1]
    rendered_html = _VERIFY_MD.render(out)
    assert "[E1, E2]" in rendered_html
    assert "[C1]" in rendered_html
    assert "[Q1]" in rendered_html
    assert r"\[" not in rendered_html
    assert "&lt; 15%" in rendered_html
    assert "<a " not in rendered_html
    assert "<img " not in rendered_html


def test_markdown_table_rendered_as_escaped_plain_text() -> None:
    table = "| Device | Disk |\n|---|---|\n| acme-macbook-4 | 2.0% |"
    out = safe_md(table)
    _assert_no_active_dangerous_tokens(out)
    assert r"\|" in out
    assert "<table>" not in _VERIFY_MD.render(out)


def test_twenty_thousand_open_brackets_finishes_under_50ms() -> None:
    # Warm up once, then time 20,000 '[' characters
    safe_md("warmup")
    payload = "[" * 20_000
    t0 = time.perf_counter()
    out = safe_md(payload)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 50.0, f"safe_md took {elapsed_ms:.2f} ms on 20k '[' (limit 50 ms)"
    assert len(out) >= 20_000
    assert r"\[" in out


def test_safe_code_strips_backticks_newlines_and_controls() -> None:
    assert safe_code(None) == ""
    assert safe_code("") == ""
    assert safe_code("normal_metric") == "normal_metric"
    cleaned = safe_code("foo` ![x](//e) `bar\nbaz\r\nqux\x00")
    assert "`" not in cleaned
    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert "\x00" not in cleaned
    assert cleaned == "foo ![x](//e) bar baz qux"
