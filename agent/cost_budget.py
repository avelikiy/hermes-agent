"""Per-session spend cap — the money axis of the iteration budget.

``IterationBudget`` bounds how many times an agent may call the model. That is
not the same as bounding what it spends: 60 iterations on a cheap model and 60
on a flagship differ by orders of magnitude, and a loop that stalls on an
expensive rung burns the account long before it burns its iteration count. A
deployment that runs unattended — cron jobs, gateway sessions, auto-approved
subagents — has nobody watching the meter.

So this caps the other axis. ``agent.max_session_cost_usd`` in config.yaml sets
a per-session ceiling in USD; the conversation loop checks it before each API
call and stops cleanly when the session has spent that much.

Deliberately per-session, not global: a global cap needs shared state across
processes and a reset policy, and gets it wrong in both directions (one long
legitimate session trips it; a hundred short runaway ones don't). A per-session
ceiling bounds the blast radius of any single runaway turn, which is the
failure this guards against.

Off by default (0 / absent = no cap), because a cap that fires unexpectedly
mid-task is its own kind of outage.
"""

from __future__ import annotations

from typing import Any, Optional


def parse_limit(raw: Any) -> Optional[float]:
    """Normalise a configured limit to a positive float, or None for "no cap".

    Accepts int/float/numeric string so a YAML value quoted by hand still
    works. Zero, negatives and junk all mean "no cap" rather than "cap at
    zero" — a misconfigured limit must not brick every turn.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0 or val != val or val == float("inf"):  # NaN/inf guard
        return None
    return val


def limit_reached(spent_usd: Any, limit_usd: Optional[float]) -> bool:
    """True when this session has spent its allowance.

    Checked *before* an API call, so the cap bounds what the session commits
    to, not what it has already paid for. The overshoot is at most one call —
    the true cost of a call is only known after it returns.
    """
    if limit_usd is None:
        return False
    try:
        spent = float(spent_usd or 0.0)
    except (TypeError, ValueError):
        return False
    if spent != spent:  # NaN
        return False
    return spent >= limit_usd


def format_message(spent_usd: Any, limit_usd: Optional[float]) -> str:
    """Operator-facing explanation. Says what to change, not just what broke."""
    try:
        spent = float(spent_usd or 0.0)
    except (TypeError, ValueError):
        spent = 0.0
    lim = f"${limit_usd:.2f}" if limit_usd is not None else "—"
    return (
        f"Session spend limit reached (${spent:.4f} of {lim}). "
        "Stopping before the next model call. "
        "Raise or clear agent.max_session_cost_usd in config.yaml to continue."
    )
