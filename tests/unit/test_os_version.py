"""Unit tests for OS version parsing and comparison."""

from __future__ import annotations

import pytest

from fleet_copilot.domain.os_version import older_than, parse_os


def test_parse_os_exact_tuples() -> None:
    v1 = parse_os("macOS", "14.5")
    assert v1 is not None
    assert v1.family == "macos"
    assert v1.parts == (14, 5)

    v2 = parse_os("macOS", "15")
    assert v2 is not None
    assert v2.family == "macos"
    assert v2.parts == (15, 0)

    w1 = parse_os("Windows", "10 22H2")
    assert w1 is not None
    assert w1.family == "windows"
    assert w1.parts == (10, 22, 2)

    w2 = parse_os("Windows", "11 23H2")
    assert w2 is not None
    assert w2.family == "windows"
    assert w2.parts == (11, 23, 2)


def test_macos_ordering_and_older_than() -> None:
    v13_6 = parse_os("macOS", "13.6")
    v14_5 = parse_os("macOS", "14.5")
    v15 = parse_os("macOS", "15")
    v15_0 = parse_os("macOS", "15.0")
    v15_3 = parse_os("macOS", "15.3")
    v15_4 = parse_os("macOS", "15.4")

    assert v13_6 is not None and v14_5 is not None
    assert v15 is not None and v15_0 is not None
    assert v15_3 is not None and v15_4 is not None

    assert v13_6 < v14_5 < v15
    assert v15 == v15_0
    assert v15_0 < v15_3 < v15_4

    assert older_than(v13_6, "macos", "15") is True
    assert older_than(v14_5, "macos", "15") is True
    assert older_than(v15, "macos", "15") is False
    assert older_than(v15_0, "macos", "15") is False
    assert older_than(v15_3, "macos", "15") is False
    assert older_than(v15_4, "macos", "15.4") is False
    assert older_than(v15_3, "macos", "15.4") is True


def test_windows_ordering_and_older_than() -> None:
    w10 = parse_os("Windows", "10 22H2")
    w11_22 = parse_os("Windows", "11 22H2")
    w11_23 = parse_os("Windows", "11 23H2")

    assert w10 is not None and w11_22 is not None and w11_23 is not None
    assert w10 < w11_22 < w11_23

    assert older_than(w10, "windows", "11 22H2") is True
    assert older_than(w11_22, "windows", "11 23H2") is True
    assert older_than(w11_23, "windows", "11 23H2") is False


def test_cross_family_and_garbage_return_none() -> None:
    win = parse_os("Windows", "10 22H2")
    mac = parse_os("macOS", "14.5")

    assert older_than(win, "macos", "15") is None
    assert older_than(mac, "windows", "11 22H2") is None
    assert older_than(None, "macos", "15") is None
    assert older_than(mac, "macos", "not-a-version") is None

    for prod, ver in (
        (None, "14.5"),
        ("macOS", None),
        ("", ""),
        ("Ubuntu", "24.04"),
        ("macOS", "Sonoma"),
        ("Windows", "XP"),
        ("Windows", "11 99X9"),
    ):
        assert parse_os(prod, ver) is None

    with pytest.raises(TypeError):
        assert win is not None and mac is not None
        _ = win < mac
