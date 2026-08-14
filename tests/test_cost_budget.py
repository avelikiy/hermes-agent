"""Per-session spend cap.

The iteration budget bounds call count, not spend — 60 calls on a cheap model
and 60 on a flagship differ by orders of magnitude. These cover the money axis:
a misconfigured cap must never brick a turn, and a real one must stop the
session before the next call rather than after the bill.
"""

from __future__ import annotations

import pytest

from agent.cost_budget import format_message, limit_reached, parse_limit


# ── parse_limit ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (2.5, 2.5), (10, 10.0), ("1.5", 1.5), ("  3 ", 3.0),
])
def test_parse_limit_accepts_numbers_and_numeric_strings(raw, expected):
    """YAML values are sometimes quoted by hand; both forms must work."""
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", [
    None, 0, 0.0, -1, -0.5, "", "abc", "$5", [], {}, float("nan"), float("inf"),
    True, False,  # bools are ints in Python — must not read as a cap of 1
])
def test_parse_limit_treats_junk_as_uncapped(raw):
    """A broken limit means "no cap", never "cap at zero" — otherwise a typo
    in config.yaml would stop every turn before its first call."""
    assert parse_limit(raw) is None


# ── limit_reached ────────────────────────────────────────────────────────────


def test_no_limit_never_trips():
    assert limit_reached(999.0, None) is False


@pytest.mark.parametrize("spent,limit,expected", [
    (0.0, 2.0, False),
    (1.99, 2.0, False),
    (2.0, 2.0, True),      # at the cap: stop, don't spend past it
    (2.01, 2.0, True),
    (100.0, 2.0, True),
])
def test_limit_trips_at_or_above_the_cap(spent, limit, expected):
    assert limit_reached(spent, limit) is expected


@pytest.mark.parametrize("spent", [None, "", "abc", [], float("nan")])
def test_unreadable_spend_does_not_trip(spent):
    """An unreadable counter must not halt the agent — it would turn a
    telemetry glitch into an outage."""
    assert limit_reached(spent, 2.0) is False


def test_zero_spend_with_limit_is_fine():
    assert limit_reached(0, 5.0) is False


# ── format_message ───────────────────────────────────────────────────────────


def test_message_names_the_setting_to_change():
    msg = format_message(2.3456, 2.0)
    assert "$2.3456" in msg and "$2.00" in msg
    assert "max_session_cost_usd" in msg


def test_message_survives_a_broken_counter():
    assert "$0.0000" in format_message(None, 1.0)


@pytest.mark.parametrize("limit,expected", [
    (2.0, "$2.00"),
    (0.5, "$0.50"),
    (0.0001, "$0.0001"),   # two decimals would render this as "$0.00"
    (0.000001, "$0.000001"),
])
def test_small_limits_survive_formatting(limit, expected):
    """A cap shown as $0.00 reads as a broken config rather than the value set."""
    assert expected in format_message(0.003, limit)


def test_no_limit_renders_as_dash():
    assert "—" in format_message(1.0, None)
