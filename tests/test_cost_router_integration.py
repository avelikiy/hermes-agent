"""Integration tests for the cost-router wire in AIAgent.run_conversation.

These exercise the forwarder glue (route → override self.model → restore) with
a stub `self` and a monkeypatched conversation loop — no real LLM calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_agent
from agent.cost_router import CostRouter, LadderEntry, Tier


def _ladder():
    return [
        LadderEntry("flash", 0.1, Tier.SIMPLE),
        LadderEntry("opus", 15.0, Tier.HARD),
    ]


def _stub(router):
    ns = SimpleNamespace(model="opus", provider="anthropic", base_url=None, _cost_router=router)
    # The forwarder calls self._get_cost_router(); provide it on the stub.
    ns._get_cost_router = lambda: router
    return ns


@pytest.fixture
def record_loop(monkeypatch):
    """Patch the real conversation loop with a recorder that captures the model
    in effect at call time."""
    seen = {}

    def fake_run_conversation(self, user_message, *a, **k):
        seen["model_at_call"] = self.model
        seen["user_message"] = user_message
        return {"final_response": "ok"}

    import agent.conversation_loop as cl
    monkeypatch.setattr(cl, "run_conversation", fake_run_conversation)
    return seen


def test_disabled_router_does_not_touch_model(record_loop):
    stub = _stub(None)
    out = run_agent.AIAgent.run_conversation(stub, "what is 2+2")
    assert out["final_response"] == "ok"
    assert record_loop["model_at_call"] == "opus"  # unchanged
    assert stub.model == "opus"


def test_simple_task_routes_to_cheap_and_restores(record_loop):
    stub = _stub(CostRouter(ladder=_ladder()))
    run_agent.AIAgent.run_conversation(stub, "what is 2+2")
    assert record_loop["model_at_call"] == "flash"  # routed cheap during turn
    assert stub.model == "opus"  # restored after


def test_hard_task_stays_on_flagship(record_loop):
    stub = _stub(CostRouter(ladder=_ladder()))
    run_agent.AIAgent.run_conversation(stub, "refactor the whole architecture")
    # routed == original "opus" → no override needed
    assert record_loop["model_at_call"] == "opus"
    assert stub.model == "opus"


def test_model_restored_even_on_exception(monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("loop blew up")

    import agent.conversation_loop as cl
    monkeypatch.setattr(cl, "run_conversation", boom)

    stub = _stub(CostRouter(ladder=_ladder()))
    with pytest.raises(RuntimeError):
        run_agent.AIAgent.run_conversation(stub, "what is 2+2")
    assert stub.model == "opus"  # restored despite the failure


def test_outcome_is_recorded(monkeypatch, tmp_path):
    from pathlib import Path

    log = tmp_path / "router_log.jsonl"
    router = CostRouter(ladder=_ladder(), log_path=log)

    def fake(self, *a, **k):
        return {"final_response": "ok"}

    import agent.conversation_loop as cl
    monkeypatch.setattr(cl, "run_conversation", fake)

    stub = _stub(router)
    run_agent.AIAgent.run_conversation(stub, "what is 2+2")
    assert log.exists()
    assert '"model": "flash"' in log.read_text()
    assert '"success": true' in log.read_text()


def test_get_cost_router_returns_none_when_config_absent(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"model": {"default": "x"}})
    stub = SimpleNamespace(model="opus", provider="anthropic", base_url=None)
    # bound-method call via the class with our stub as self
    assert run_agent.AIAgent._get_cost_router(stub) is None
    assert stub._cost_router is None  # cached


def test_get_cost_router_builds_when_enabled(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(
        cfg,
        "load_config_readonly",
        lambda: {"routing": {"enabled": True, "models": ["claude-haiku-4-5", "claude-opus-4-8"], "log": ""}},
    )
    stub = SimpleNamespace(model="claude-opus-4-8", provider="anthropic", base_url=None)
    router = run_agent.AIAgent._get_cost_router(stub)
    assert isinstance(router, CostRouter)
    assert router.ladder  # built a non-empty ladder from real pricing


# ── outcome-based escalation ─────────────────────────────────────────────────


def _esc_router(tmp_path, **kw):
    """Router whose cheapest rung clears every tier, so routing is predictable
    and the test isolates escalation behaviour."""
    return CostRouter(
        ladder=[LadderEntry("flash", 0.1, Tier.HARD), LadderEntry("opus", 15.0, Tier.HARD)],
        fallback_model="opus",
        log_path=tmp_path / "router.jsonl",
        **kw,
    )


def _run(monkeypatch, router, results):
    """Drive the forwarder with a scripted sequence of loop results."""
    calls = []

    def fake_run_conversation(self, user_message, system_message=None,
                              conversation_history=None, task_id=None,
                              stream_callback=None, persist_user_message=True, *a, **k):
        calls.append({"model": self.model, "persist": persist_user_message})
        return results[len(calls) - 1]

    import agent.conversation_loop as cl
    monkeypatch.setattr(cl, "run_conversation", fake_run_conversation)
    out = run_agent.AIAgent.run_conversation(_stub(router), "нужен ответ")
    return calls, out


def test_failed_turn_escalates_one_rung(monkeypatch, tmp_path):
    router = _esc_router(tmp_path, escalate_on_failure=True, max_escalations=1)
    empty = {"final_response": "", "messages": []}
    good = {"final_response": "готово", "messages": []}
    calls, out = _run(monkeypatch, router, [empty, good])

    assert [c["model"] for c in calls] == ["flash", "opus"]
    assert out is good
    # The user turn was already persisted by the first attempt.
    assert calls[1]["persist"] is False


def test_successful_turn_does_not_escalate(monkeypatch, tmp_path):
    router = _esc_router(tmp_path, escalate_on_failure=True, max_escalations=1)
    good = {"final_response": "готово", "messages": []}
    calls, _ = _run(monkeypatch, router, [good])
    assert [c["model"] for c in calls] == ["flash"]


def test_no_escalation_when_disabled(monkeypatch, tmp_path):
    router = _esc_router(tmp_path, escalate_on_failure=False)
    empty = {"final_response": "", "messages": []}
    calls, _ = _run(monkeypatch, router, [empty])
    assert [c["model"] for c in calls] == ["flash"]


def test_no_escalation_after_tool_side_effects(monkeypatch, tmp_path):
    """A failed turn that already ran tools must not be replayed."""
    router = _esc_router(tmp_path, escalate_on_failure=True, max_escalations=1)
    with_tools = {"final_response": "", "messages": [{"role": "tool", "content": "wrote"}]}
    calls, _ = _run(monkeypatch, router, [with_tools])
    assert [c["model"] for c in calls] == ["flash"]


def test_escalation_budget_is_respected(monkeypatch, tmp_path):
    """max_escalations caps retries even when more rungs remain."""
    router = CostRouter(
        ladder=[LadderEntry("flash", 0.1, Tier.HARD),
                LadderEntry("mid", 1.0, Tier.HARD),
                LadderEntry("opus", 15.0, Tier.HARD)],
        fallback_model="opus", log_path=tmp_path / "r.jsonl",
        escalate_on_failure=True, max_escalations=1,
    )
    empty = {"final_response": "", "messages": []}
    calls, _ = _run(monkeypatch, router, [empty, empty])
    assert [c["model"] for c in calls] == ["flash", "mid"]


def test_outcomes_are_logged_for_every_attempt(monkeypatch, tmp_path):
    import json
    router = _esc_router(tmp_path, escalate_on_failure=True, max_escalations=1)
    empty = {"final_response": "", "messages": []}
    good = {"final_response": "готово", "messages": []}
    _run(monkeypatch, router, [empty, good])

    rows = [json.loads(l) for l in router.log_path.read_text().splitlines() if l.strip()]
    assert [(r["model"], r["success"], r["escalated"]) for r in rows] == [
        ("flash", False, False),
        ("opus", True, True),
    ]


def test_crash_is_recorded_as_failure(monkeypatch, tmp_path):
    import json
    router = _esc_router(tmp_path, escalate_on_failure=True)

    def boom(self, user_message, *a, **k):
        raise RuntimeError("provider exploded")

    import agent.conversation_loop as cl
    monkeypatch.setattr(cl, "run_conversation", boom)
    with pytest.raises(RuntimeError):
        run_agent.AIAgent.run_conversation(_stub(router), "hi")

    rows = [json.loads(l) for l in router.log_path.read_text().splitlines() if l.strip()]
    assert rows[-1]["success"] is False
