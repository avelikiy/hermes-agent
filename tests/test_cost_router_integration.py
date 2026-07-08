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
