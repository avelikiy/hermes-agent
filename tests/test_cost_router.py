"""Unit tests for agent.cost_router (offline, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.cost_router import (
    CostRouter,
    LadderEntry,
    Tier,
    build_ladder,
    capability_tier,
    classify_task,
    from_config,
)


# ── classify_task ────────────────────────────────────────────────────────────


def test_classify_simple_lookup():
    assert classify_task("what is the capital of France") is Tier.SIMPLE


def test_classify_simple_short_chitchat():
    assert classify_task("hi there") is Tier.SIMPLE


def test_classify_hard_keyword():
    assert classify_task("Please refactor this module for clarity") is Tier.HARD


def test_classify_hard_code_fence():
    assert classify_task("fix this:\n```\ndef f(): return 1\n```") is Tier.HARD


def test_classify_hard_long_message():
    assert classify_task("x " * 1500) is Tier.HARD


def test_classify_hard_images_beats_simple_text():
    # "what is" would be SIMPLE, but an attached image forces HARD.
    assert classify_task("what is this", has_images=True) is Tier.HARD


def test_classify_hard_many_tools():
    assert classify_task("do a thing", tool_names=["a", "b", "c"]) is Tier.HARD


def test_classify_medium_default():
    assert classify_task("Can you help me think about my weekend plans a bit?") is Tier.MEDIUM


def test_classify_custom_thresholds():
    # With a tiny long_threshold everything long becomes HARD.
    assert classify_task("a" * 50, long_threshold=10) is Tier.HARD


# ── capability_tier ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-4-8", Tier.HARD),
        ("claude-3-5-haiku", Tier.MEDIUM),
        ("gpt-4o-mini", Tier.MEDIUM),
        ("llama-3.1-8b-instruct", Tier.SIMPLE),
        ("some-unknown-model", Tier.MEDIUM),  # safe default
    ],
)
def test_capability_tier(model, expected):
    assert capability_tier(model) is expected


def test_capability_tier_override():
    assert capability_tier("my-local-7b", overrides=[("my-local-7b", Tier.HARD)]) is Tier.HARD


# ── build_ladder ─────────────────────────────────────────────────────────────


def _cost_fn(mapping):
    return lambda m: mapping.get(m)


def test_build_ladder_sorts_ascending_by_cost():
    costs = {"cheap": 0.1, "mid": 1.0, "exp": 15.0}
    ladder = build_ladder(["exp", "cheap", "mid"], _cost_fn(costs))
    assert [e.model for e in ladder] == ["cheap", "mid", "exp"]


def test_build_ladder_unknown_cost_sorts_last():
    ladder = build_ladder(["known", "mystery"], _cost_fn({"known": 1.0}))
    assert ladder[-1].model == "mystery"
    assert ladder[-1].cost == float("inf")


def test_build_ladder_dedupes():
    ladder = build_ladder(["a", "a", "b"], _cost_fn({"a": 1.0, "b": 2.0}))
    assert [e.model for e in ladder] == ["a", "b"]


# ── routing ──────────────────────────────────────────────────────────────────


def _router():
    ladder = [
        LadderEntry("flash", 0.1, Tier.SIMPLE),
        LadderEntry("haiku", 1.0, Tier.MEDIUM),
        LadderEntry("opus", 15.0, Tier.HARD),
    ]
    return CostRouter(ladder=ladder)


def test_route_simple_picks_cheapest():
    assert _router().route("what is 2+2") == "flash"


def test_route_hard_picks_capable_not_cheapest():
    assert _router().route("refactor the auth architecture") == "opus"


def test_route_medium_picks_mid():
    assert _router().route("help me think about my weekend plans a little") == "haiku"


def test_route_empty_ladder_returns_none():
    assert CostRouter(ladder=[]).route("anything") is None


def test_route_when_nothing_clears_bar_uses_most_capable():
    # Only cheap low-cap models available, but task is HARD.
    ladder = [LadderEntry("flash", 0.1, Tier.SIMPLE), LadderEntry("flash2", 0.2, Tier.SIMPLE)]
    r = CostRouter(ladder=ladder)
    assert r.route("prove this theorem step by step") in {"flash", "flash2"}


# ── escalation ───────────────────────────────────────────────────────────────


def test_escalate_walks_up():
    r = _router()
    assert r.escalate("flash") == "haiku"
    assert r.escalate("haiku") == "opus"


def test_escalate_at_top_returns_none():
    assert _router().escalate("opus") is None


def test_escalate_unknown_model_starts_from_bottom():
    assert _router().escalate("not-in-ladder") == "flash"


# ── record_outcome ───────────────────────────────────────────────────────────


def test_record_outcome_writes_jsonl(tmp_path: Path):
    log = tmp_path / "router_log.jsonl"
    r = CostRouter(ladder=_router().ladder, log_path=log)
    r.record_outcome(message="hi", model="flash", tier=Tier.SIMPLE, success=True)
    r.record_outcome(message="refactor", model="opus", tier=Tier.HARD, success=False, escalated=True)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["model"] == "flash" and first["success"] is True
    second = json.loads(lines[1])
    assert second["escalated"] is True and second["tier"] == int(Tier.HARD)


def test_record_outcome_no_log_path_is_noop():
    CostRouter(ladder=[]).record_outcome(message="x", model="m", tier=Tier.SIMPLE, success=True)


# ── from_config ──────────────────────────────────────────────────────────────


def test_from_config_disabled_returns_none():
    assert from_config({"routing": {"enabled": False}}) is None
    assert from_config({}) is None
    assert from_config(None) is None


def test_from_config_enabled_builds_router():
    cfg = {
        "routing": {
            "enabled": True,
            "models": ["flash", "opus"],
            "log": "",  # disable logging
        }
    }
    r = from_config(cfg, cost_fn=_cost_fn({"flash": 0.1, "opus": 15.0}))
    assert isinstance(r, CostRouter)
    assert [e.model for e in r.ladder] == ["flash", "opus"]
    assert r.log_path is None


def test_from_config_appends_main_model():
    cfg = {"routing": {"enabled": True, "models": ["flash"], "log": ""}}
    r = from_config(cfg, main_model="opus", cost_fn=_cost_fn({"flash": 0.1, "opus": 15.0}))
    assert "opus" in [e.model for e in r.ladder]


def test_from_config_no_models_disables():
    assert from_config({"routing": {"enabled": True, "models": []}}) is None


def test_from_config_tier_overrides_and_classify():
    cfg = {
        "routing": {
            "enabled": True,
            "models": ["weird-model"],
            "model_tiers": {"weird-model": "HARD"},
            "classify": {"long_threshold": 10},
            "log": "",
        }
    }
    r = from_config(cfg, cost_fn=_cost_fn({"weird-model": 1.0}))
    assert r.ladder[0].cap is Tier.HARD
    assert r.classify_kwargs.get("long_threshold") == 10


def test_from_config_extends_hard_keywords():
    """routing.hard_keywords must EXTEND the built-in list, not replace it.

    The built-ins are English-only, so a Russian deployment classified every
    turn as SIMPLE/MEDIUM and never reached the HARD rungs — the flagship was
    unreachable in practice. Operators add their own stems; the English ones
    must keep working alongside.
    """
    cfg = {
        "routing": {
            "enabled": True,
            "models": ["flash", "opus"],
            "log": "",
            "hard_keywords": ["отрефактор", "докажи"],
        }
    }
    r = from_config(cfg, cost_fn=_cost_fn({"flash": 0.1, "opus": 15.0}))
    # operator-supplied stem now reaches the HARD rung
    assert r.route("отрефактори модуль и докажи инвариант") == "opus"
    # built-in English signal still works
    assert r.route("refactor this module and prove the invariant") == "opus"
    # unrelated short lookup stays cheap
    assert r.route("что такое MTU") == "flash"


def test_from_config_extends_simple_keywords():
    cfg = {
        "routing": {
            "enabled": True,
            "models": ["flash", "opus"],
            "log": "",
            "simple_keywords": ["переведи"],
        }
    }
    r = from_config(cfg, cost_fn=_cost_fn({"flash": 0.1, "opus": 15.0}))
    assert classify_task(
        "переведи это на английский",
        simple_keywords=r.classify_kwargs["simple_keywords"],
    ) is Tier.SIMPLE
    # built-in simple signal survives
    assert classify_task(
        "what is the capital of France",
        simple_keywords=r.classify_kwargs["simple_keywords"],
    ) is Tier.SIMPLE


def test_from_config_ignores_empty_keyword_lists():
    """Empty/absent lists must leave the defaults untouched."""
    cfg = {"routing": {"enabled": True, "models": ["flash"], "log": "",
                       "hard_keywords": [], "simple_keywords": None}}
    r = from_config(cfg, cost_fn=_cost_fn({"flash": 0.1}))
    assert "hard_keywords" not in r.classify_kwargs
    assert "simple_keywords" not in r.classify_kwargs


# ── turn_succeeded ───────────────────────────────────────────────────────────


def test_turn_succeeded_real_answer():
    assert CostRouter.turn_succeeded(
        {"final_response": "готово", "completed": True, "failed": False}
    ) is True


@pytest.mark.parametrize("result", [
    {"final_response": None, "completed": False, "failed": True, "error": "boom"},
    {"final_response": "", "completed": True, "failed": False},
    {"final_response": "   ", "completed": True, "failed": False},
    {"final_response": "ok", "completed": False},
    {"final_response": "ok", "error": "rate limited"},
])
def test_turn_succeeded_rejects_non_answers(result):
    """An empty or failed turn must not be logged as a success — that is the
    signal Phase 3 trains on."""
    assert CostRouter.turn_succeeded(result) is False


def test_turn_succeeded_plain_string_paths():
    assert CostRouter.turn_succeeded("some answer") is True
    assert CostRouter.turn_succeeded("") is False


def test_turn_succeeded_unknown_shape_is_lenient():
    """Unknown result types degrade to the old lenient behaviour rather than
    flooding the log with false failures."""
    assert CostRouter.turn_succeeded(object()) is True
    assert CostRouter.turn_succeeded({"messages": []}) is True
