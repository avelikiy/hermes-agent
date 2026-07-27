"""Gateway must forward the ``checkpoints`` config block to AIAgent.

Regression guard: AIAgent takes ``checkpoints_enabled`` as a parameter that
defaults to False and never reads config itself. The gateway used to omit it
entirely, so ``checkpoints: {enabled: true}`` in config.yaml silently did
nothing for gateway sessions (Telegram, cron, subagents) and only worked for
``hermes chat``. That is backwards: unattended sessions are precisely where an
undo buffer matters.
"""

from __future__ import annotations

import pytest

from gateway.run import GatewayRunner


kwargs = GatewayRunner._checkpoint_kwargs


def test_disabled_yields_no_kwargs():
    """Disabled must produce {} so AIAgent keeps its own defaults."""
    assert kwargs({"checkpoints": {"enabled": False}}) == {}


def test_missing_section_yields_no_kwargs():
    assert kwargs({}) == {}


def test_enabled_forwards_flag_and_limits():
    out = kwargs({
        "checkpoints": {
            "enabled": True,
            "max_snapshots": 20,
            "max_total_size_mb": 500,
            "max_file_size_mb": 10,
        }
    })
    assert out == {
        "checkpoints_enabled": True,
        "checkpoint_max_snapshots": 20,
        "checkpoint_max_total_size_mb": 500,
        "checkpoint_max_file_size_mb": 10,
    }


def test_enabled_without_limits_forwards_only_the_flag():
    """Absent limits must not be forwarded as None and clobber AIAgent's defaults."""
    assert kwargs({"checkpoints": {"enabled": True}}) == {"checkpoints_enabled": True}


@pytest.mark.parametrize("bad", [0, -1, "20", None, 1.5])
def test_invalid_limits_are_skipped(bad):
    out = kwargs({"checkpoints": {"enabled": True, "max_snapshots": bad}})
    assert out == {"checkpoints_enabled": True}


@pytest.mark.parametrize("cfg", ["broken", 42, {"checkpoints": "broken"}, {"checkpoints": None}])
def test_malformed_config_never_raises(cfg):
    """Checkpoints are a safety net — a bad config must not break message handling."""
    assert kwargs(cfg) == {}
