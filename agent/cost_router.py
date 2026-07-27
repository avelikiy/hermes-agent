"""Cost-cascade router — pick the cheapest model likely to clear the quality bar.

Inspired by ``@metaharness/router`` (ruvnet/agent-harness-generator), adapted to
Hermes' provider/pricing layer.

The idea: rather than always running the user's flagship model, classify each
incoming task by difficulty, then route to the *cheapest* model whose capability
clears that difficulty. If the cheap model fails (error, or the caller asks to
retry), cascade one rung up the cost ladder. Most turns are cheap; only the hard
or failing ones reach the expensive model.

Opt-in. With ``routing.enabled: false`` (the default) :func:`from_config`
returns ``None`` and the agent's normal model selection is untouched — so this
module is inert until a user turns it on in ``config.yaml``.

Phase 1 (this module) is rule-based and fully offline-testable: the ladder is
built once from :mod:`agent.usage_pricing`, classification is cheap heuristics,
and routing/escalation are pure functions over the ladder. Phase 3 will learn
the tier→model mapping from ``~/.hermes/router_log.jsonl`` outcomes recorded by
:meth:`CostRouter.record_outcome`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


class Tier(IntEnum):
    """Task difficulty / model capability, ordered so comparisons work.

    A model can serve a task when its capability tier is >= the task tier.
    """

    SIMPLE = 0
    MEDIUM = 1
    HARD = 2


# ── Task classification ──────────────────────────────────────────────────────

# Signals that a task needs real reasoning. Kept transparent and tunable rather
# than a black box — a user can extend these via routing.hard_keywords in config.
_HARD_KEYWORDS = (
    "refactor", "architecture", "architect", "design ", "prove", "proof",
    "debug", "root cause", "step by step", "step-by-step", "plan ", "algorithm",
    "optimize", "optimise", "derive", "trade-off", "tradeoff", "analyze deeply",
    "rewrite", "implement", "migrate", "benchmark",
)
# Signals that a task is shallow lookup / chit-chat / formatting.
_SIMPLE_KEYWORDS = (
    "what is", "who is", "define", "translate", "summarize", "summarise",
    "tl;dr", "rename", "list ", "convert", "spell", "capital of",
)
_CODE_FENCE = re.compile(r"```|\bdef \b|\bclass \b|function\s|=>|;\s*$", re.MULTILINE)


def classify_task(
    message: str,
    *,
    tool_names: Iterable[str] = (),
    has_images: bool = False,
    long_threshold: int = 1800,
    short_threshold: int = 280,
    hard_keywords: Iterable[str] = (),
    simple_keywords: Iterable[str] = (),
) -> Tier:
    """Classify a user turn into a difficulty tier with cheap heuristics.

    The order of checks matters: strong HARD signals (length, images, code,
    reasoning keywords) win over SIMPLE signals, so "summarize this 5000-line
    refactor plan" is treated as HARD, not SIMPLE.
    """
    text = (message or "").strip()
    low = text.lower()
    tools = list(tool_names)

    hard_kw = tuple(hard_keywords) or _HARD_KEYWORDS
    simple_kw = tuple(simple_keywords) or _SIMPLE_KEYWORDS

    # --- HARD signals -------------------------------------------------------
    if has_images:
        return Tier.HARD
    if len(text) >= long_threshold:
        return Tier.HARD
    if len(tools) >= 3:
        return Tier.HARD
    if _CODE_FENCE.search(text):
        return Tier.HARD
    if any(kw in low for kw in hard_kw):
        return Tier.HARD

    # --- SIMPLE signals -----------------------------------------------------
    # Short + no tools, AND either a lookup/chit-chat keyword or trivially tiny.
    # We deliberately do NOT mark every short message SIMPLE: a 2-line question
    # can still need real reasoning, so length alone is not enough.
    if len(text) <= short_threshold and not tools:
        if any(low.startswith(kw) or kw in low for kw in simple_kw):
            return Tier.SIMPLE
        if len(text) <= 40:  # greetings / one-liners
            return Tier.SIMPLE

    return Tier.MEDIUM


# ── Model capability tiers ───────────────────────────────────────────────────

# Pattern → capability tier. First match wins (longest/most-specific patterns
# first). Overridable via routing.model_tiers in config.yaml.
# NOTE: order matters — size/variant markers (mini, nano, 8b, lite, air, …)
# come BEFORE family names (gpt-4o, gemini, glm-4.5, …) so that e.g.
# "gpt-4o-mini" resolves to MEDIUM via "mini", not HARD via "gpt-4o".
_DEFAULT_MODEL_TIERS: tuple[tuple[str, Tier], ...] = (
    # --- small / cheap variants (matched first) ---
    ("flash-lite", Tier.SIMPLE),
    ("nano", Tier.SIMPLE),
    ("8b", Tier.SIMPLE),
    ("7b", Tier.SIMPLE),
    ("3b", Tier.SIMPLE),
    ("-lite", Tier.SIMPLE),
    # --- mid-tier variants (matched before family names) ---
    ("mini", Tier.MEDIUM),
    ("haiku", Tier.MEDIUM),
    ("air", Tier.MEDIUM),
    ("flash", Tier.MEDIUM),
    ("32b", Tier.MEDIUM),
    # --- flagship / deep-reasoning families ---
    ("opus", Tier.HARD),
    ("gpt-5", Tier.HARD),
    ("gpt-4.1", Tier.HARD),
    ("gpt-4o", Tier.HARD),
    ("o1", Tier.HARD),
    ("o3", Tier.HARD),
    ("sonnet", Tier.HARD),
    ("deepseek-r", Tier.HARD),
    ("glm-4.6", Tier.HARD),
    ("glm-4.5", Tier.HARD),
    ("kimi", Tier.HARD),
    ("405b", Tier.HARD),
    ("70b", Tier.HARD),
    ("gemini-1.5-pro", Tier.HARD),
    ("gemini", Tier.MEDIUM),
)


def capability_tier(
    model: str, overrides: Iterable[tuple[str, Tier]] = ()
) -> Tier:
    """Map a model name to the difficulty it can reliably handle.

    Unknown models default to MEDIUM (a safe middle: not trusted with the
    hardest tasks, not assumed useless).
    """
    name = (model or "").lower()
    for pattern, tier in list(overrides) + list(_DEFAULT_MODEL_TIERS):
        if pattern.lower() in name:
            return tier
    return Tier.MEDIUM


# ── Cost ladder ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LadderEntry:
    model: str
    cost: float  # blended $/Mtok; float('inf') when unknown (sorts last)
    cap: Tier


# A cost function: (model) -> blended $/Mtok or None. Injectable for tests.
CostFn = Callable[[str], Optional[float]]


def _default_cost_fn(provider: Optional[str], base_url: Optional[str]) -> CostFn:
    """Blended cost via usage_pricing. Weights input heavier than output since
    most agent turns are read-heavy (large context, short completion)."""

    def cost_fn(model: str) -> Optional[float]:
        try:
            from agent.usage_pricing import get_pricing_entry  # lazy: avoid cycle
        except Exception:  # pragma: no cover - import guard
            return None
        try:
            entry = get_pricing_entry(model, provider=provider, base_url=base_url)
        except Exception:
            return None
        if entry is None:
            return None
        inp = entry.input_cost_per_million
        out = entry.output_cost_per_million
        if inp is None and out is None:
            return None
        inp_f = float(inp) if inp is not None else 0.0
        out_f = float(out) if out is not None else 0.0
        return 0.75 * inp_f + 0.25 * out_f

    return cost_fn


def build_ladder(
    models: Iterable[str],
    cost_fn: CostFn,
    tier_overrides: Iterable[tuple[str, Tier]] = (),
) -> list[LadderEntry]:
    """Build a cost-ascending ladder. Unknown-cost models sort last but stay
    usable as a last resort. De-duplicates by model name (first wins)."""
    seen: set[str] = set()
    entries: list[LadderEntry] = []
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        cost = cost_fn(model)
        entries.append(
            LadderEntry(
                model=model,
                cost=float(cost) if cost is not None else float("inf"),
                cap=capability_tier(model, tier_overrides),
            )
        )
    entries.sort(key=lambda e: (e.cost, e.cap))
    return entries


# ── Router ───────────────────────────────────────────────────────────────────


@dataclass
class CostRouter:
    """Routes turns to the cheapest capable model and cascades on failure."""

    ladder: list[LadderEntry]
    fallback_model: Optional[str] = None
    log_path: Optional[Path] = None
    classify_kwargs: dict = field(default_factory=dict)
    # Outcome-based escalation: retry a failed turn one rung up instead of
    # relying solely on the up-front difficulty guess. Off by default — it can
    # double the cost of a turn, and only pays off where retries are safe.
    escalate_on_failure: bool = False
    max_escalations: int = 1

    def route(
        self,
        message: str,
        *,
        tool_names: Iterable[str] = (),
        has_images: bool = False,
    ) -> Optional[str]:
        """Return the cheapest model whose capability clears the task tier, or
        ``None`` (let the caller use its default) when the ladder is empty."""
        if not self.ladder:
            return None
        tier = classify_task(
            message,
            tool_names=tool_names,
            has_images=has_images,
            **self.classify_kwargs,
        )
        for entry in self.ladder:  # ascending cost
            if entry.cap >= tier:
                logger.debug("cost_router: task=%s -> %s ($%.3f)", tier.name, entry.model, entry.cost)
                return entry.model
        # Nothing clears the bar → use the most capable (last by cap) we have.
        best = max(self.ladder, key=lambda e: (e.cap, -e.cost))
        return best.model

    def escalate(self, current_model: Optional[str]) -> Optional[str]:
        """Next rung up: the cheapest model strictly more expensive than the
        current one. Returns ``None`` when already at the top (caller stops)."""
        if not self.ladder:
            return None
        cur_cost = -1.0
        for e in self.ladder:
            if e.model == current_model:
                cur_cost = e.cost
                break
        for e in self.ladder:  # ascending
            if e.cost > cur_cost and e.model != current_model:
                return e.model
        return None

    @staticmethod
    def turn_succeeded(result: Any) -> bool:
        """Judge a finished turn from ``run_conversation``'s result.

        Phase 3 learns which rung is good enough for which tier, so the signal
        it trains on has to mean "the cheap model actually did the job". The
        call site used to set success=True whenever no exception escaped, which
        made every logged outcome a success (51/51 in a live deployment) and
        left the log unable to distinguish a good answer from an empty one.

        A turn counts as successful only when the loop completed, did not flag
        failure, and produced non-empty content — the same "did we get a real
        answer back" check an empty-patch cascade uses to decide whether to
        escalate. Unknown shapes are treated as success so a future change to
        the result type degrades to the old lenient behaviour rather than
        flooding the log with false failures.
        """
        if not isinstance(result, dict):
            # Older/simpler call paths return the response text directly.
            if isinstance(result, str):
                return bool(result.strip())
            return True
        if result.get("failed") or result.get("error"):
            return False
        if result.get("completed") is False:
            return False
        if "final_response" in result:
            resp = result.get("final_response")
            return bool(resp and str(resp).strip())
        return True

    @staticmethod
    def retry_is_safe(result: Any) -> bool:
        """Whether a failed turn can be re-run on a stronger model.

        Escalation re-runs the *whole* turn, so it is only safe when the first
        attempt had no effect the user or the world can see. Two things make a
        retry unsafe:

        * **Tool calls happened.** Re-running replays them — a second file
          write, a second shell command, a second outbound message. A cheaper
          answer is never worth doing someone's side effects twice.
        * **Content already reached the user.** Gateways stream tokens as they
          arrive, so a partially delivered answer cannot be taken back; a retry
          would append a second reply to the same question.

        Conservative by construction: anything unrecognised returns False, so
        an unknown result shape costs a missed optimisation rather than a
        duplicated side effect.
        """
        if not isinstance(result, dict):
            return False
        msgs = result.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if m.get("role") == "tool" or m.get("tool_calls") or m.get("tool_call_id"):
                    return False
        # A non-empty response means tokens were (or are being) delivered.
        resp = result.get("final_response")
        if resp and str(resp).strip():
            return False
        return True

    def record_outcome(
        self,
        *,
        message: str,
        model: str,
        tier: Tier,
        success: bool,
        escalated: bool = False,
        extra: Optional[dict] = None,
    ) -> None:
        """Append one eval-log line for Phase 3 learning. Best-effort: never
        raises into the turn."""
        if not self.log_path:
            return
        rec = {
            "ts": int(time.time()),
            "model": model,
            "tier": int(tier),
            "success": bool(success),
            "escalated": bool(escalated),
            "msg_len": len(message or ""),
        }
        if extra:
            rec.update(extra)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover - logging must not break turns
            logger.debug("cost_router: failed to record outcome: %s", exc)


# ── Config wiring ────────────────────────────────────────────────────────────


def from_config(
    config: Optional[dict],
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    main_model: Optional[str] = None,
    cost_fn: Optional[CostFn] = None,
) -> Optional[CostRouter]:
    """Build a :class:`CostRouter` from the ``routing:`` config block, or return
    ``None`` when routing is disabled/misconfigured (inert — caller unchanged).

    Expected config shape (all optional except ``enabled``)::

        routing:
          enabled: true
          models: ["glm-4-flash", "glm-4.6", "claude-opus-4-8"]
          model_tiers:            # name-substring -> SIMPLE|MEDIUM|HARD
            my-local-7b: SIMPLE
          log: ~/.hermes/router_log.jsonl   # set "" to disable logging
          classify:
            long_threshold: 1800
            short_threshold: 280
          escalate_on_failure: false  # retry a failed turn one rung up
          max_escalations: 1          # retries per turn, not per ladder
    """
    routing = (config or {}).get("routing") if isinstance(config, dict) else None
    if not isinstance(routing, dict) or not routing.get("enabled"):
        return None

    models = list(routing.get("models") or [])
    if main_model and main_model not in models:
        # Always keep the user's flagship reachable as the top of the ladder.
        models.append(main_model)
    if not models:
        logger.warning("routing.enabled is true but no models configured; disabling router")
        return None

    overrides = _parse_tier_overrides(routing.get("model_tiers"))
    cf = cost_fn or _default_cost_fn(provider, base_url)
    ladder = build_ladder(models, cf, overrides)

    log_path: Optional[Path] = None
    raw_log = routing.get("log", "~/.hermes/router_log.jsonl")
    if raw_log:
        log_path = Path(os.path.expanduser(str(raw_log)))

    classify_kwargs = {}
    classify_cfg = routing.get("classify")
    if isinstance(classify_cfg, dict):
        for key in ("long_threshold", "short_threshold"):
            if key in classify_cfg:
                try:
                    classify_kwargs[key] = int(classify_cfg[key])
                except (TypeError, ValueError):
                    pass

    # routing.hard_keywords / routing.simple_keywords — operator-supplied
    # difficulty signals. The built-in lists are English-only, so a non-English
    # deployment would classify every turn as SIMPLE/MEDIUM and never reach the
    # HARD rungs. We EXTEND the defaults rather than replace them, so adding
    # Russian stems keeps the English ones working.
    for key, defaults in (
        ("hard_keywords", _HARD_KEYWORDS),
        ("simple_keywords", _SIMPLE_KEYWORDS),
    ):
        raw_kw = routing.get(key)
        if isinstance(raw_kw, (list, tuple)):
            extra = tuple(
                str(k).strip().lower() for k in raw_kw if str(k).strip()
            )
            if extra:
                classify_kwargs[key] = defaults + extra

    try:
        max_esc = int(routing.get("max_escalations", 1))
    except (TypeError, ValueError):
        max_esc = 1

    return CostRouter(
        ladder=ladder,
        fallback_model=main_model,
        log_path=log_path,
        classify_kwargs=classify_kwargs,
        escalate_on_failure=bool(routing.get("escalate_on_failure", False)),
        max_escalations=max(0, max_esc),
    )


def _parse_tier_overrides(raw: Any) -> tuple[tuple[str, Tier], ...]:
    if not isinstance(raw, dict):
        return ()
    out: list[tuple[str, Tier]] = []
    for pattern, tier_name in raw.items():
        try:
            tier = Tier[str(tier_name).strip().upper()]
        except KeyError:
            logger.warning("routing.model_tiers: unknown tier %r for %r", tier_name, pattern)
            continue
        out.append((str(pattern), tier))
    return tuple(out)
