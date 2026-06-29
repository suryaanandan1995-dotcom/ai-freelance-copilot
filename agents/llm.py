"""LLM access layer for the agent pipeline.

Wraps `langchain_anthropic.ChatAnthropic` so the rest of the pipeline depends on
a single `get_chat(model, **kw)` factory. Tests run fully offline by injecting a
`FakeChat` (no API key, no network) — every agent accepts an optional `chat=`
override that defaults to `get_chat(...)`.
"""
from __future__ import annotations

import json
from typing import Any

from config import get_settings
from costs import CostTracker

# --- per-run cost tracking -----------------------------------------------------
# A pipeline run installs its CostTracker here; the metered wrapper reads it on
# every call to enforce the budget and accumulate spend. Runs are sequential per
# process, so a module global is sufficient (and easy to stub in tests).
_active_tracker: CostTracker | None = None


def set_cost_tracker(tracker: CostTracker | None) -> None:
    global _active_tracker
    _active_tracker = tracker


def get_cost_tracker() -> CostTracker | None:
    return _active_tracker


def _extract_usage(result: Any) -> tuple[int, int]:
    """Best-effort (input, output) token counts from a langchain result.

    Plain ``.invoke`` returns an AIMessage carrying ``usage_metadata``. Structured
    (`with_structured_output`) calls return a parsed model with no usage attached,
    so those report (0, 0) — the expensive plain-text drafting path is the one we
    most need to meter, and the pre-call budget gate still fires regardless.
    """
    usage = getattr(result, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    meta = getattr(result, "response_metadata", None)
    if isinstance(meta, dict):
        u = meta.get("usage") or {}
        return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
    return 0, 0


class _MeteredChat:
    """Proxy that enforces the run budget and records token usage per call."""

    def __init__(self, inner: Any, model: str) -> None:
        self._inner = inner
        self._model = model

    def invoke(self, messages: Any) -> Any:  # noqa: ANN401
        tracker = _active_tracker
        if tracker is not None:
            tracker.check()  # raises BudgetExhausted if the cap is already reached
        before = tracker.usd() if tracker is not None else 0.0
        result = self._inner.invoke(messages)
        if tracker is not None:
            in_tok, out_tok = _extract_usage(result)
            tracker.record(self._model, in_tok, out_tok)
            try:
                from observability import metrics

                metrics.inc("claude_cost_usd", max(0.0, tracker.usd() - before))
            except Exception:
                pass
        return result

    def with_structured_output(self, schema: Any, **kw: Any) -> _MeteredChat:
        return _MeteredChat(self._inner.with_structured_output(schema, **kw), self._model)

    def bind_tools(self, *a: Any, **kw: Any) -> _MeteredChat:
        return _MeteredChat(self._inner.bind_tools(*a, **kw), self._model)

    def __getattr__(self, name: str) -> Any:  # transparent proxy for anything else
        return getattr(self._inner, name)


class _FakeAIMessage:
    """Minimal stand-in for langchain's AIMessage (only `.content` is used)."""

    def __init__(self, content: str) -> None:
        self.content = content


class FakeChat:
    """Deterministic, offline stand-in for ChatAnthropic.

    Mimics the two call shapes the agents use:
      * ``.invoke(messages) -> AIMessage`` with a ``.content`` string.
      * ``.with_structured_output(Model).invoke(messages) -> Model`` instance.

    Behaviour is configured per construction so each test can pin the output:
      * ``responses``: list of strings (or dicts) returned by successive
        ``.invoke`` calls; the last one is reused once exhausted.
      * ``structured``: a callable ``(messages) -> dict`` or a fixed dict used to
        build the pydantic model passed to ``with_structured_output``.
    """

    def __init__(
        self,
        responses: list[Any] | None = None,
        structured: Any = None,
        **_: Any,
    ) -> None:
        self._responses = list(responses) if responses else ["OK"]
        self._idx = 0
        self._structured = structured
        self._schema: Any = None

    # --- plain text generation -------------------------------------------------
    def invoke(self, messages: Any) -> Any:  # noqa: ANN401 - mirrors LC signature
        if self._schema is not None:
            return self._build_structured(messages)
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        if isinstance(resp, (dict, list)):
            return _FakeAIMessage(json.dumps(resp))
        return _FakeAIMessage(str(resp))

    # --- structured output -----------------------------------------------------
    def with_structured_output(self, schema: Any, **_: Any) -> FakeChat:
        clone = FakeChat(responses=self._responses, structured=self._structured)
        clone._schema = schema
        clone._idx = self._idx
        return clone

    def _build_structured(self, messages: Any) -> Any:
        data = self._structured
        if callable(data):
            data = data(messages)
        if data is None:
            data = {}
        if isinstance(data, self._schema):  # already a model instance
            return data
        return self._schema(**data)

    def bind_tools(self, *_: Any, **__: Any) -> FakeChat:
        return self


def get_chat(model: str, *, chat: Any = None, **kw: Any) -> Any:
    """Return a chat model for ``model``.

    If ``chat`` is provided (tests inject a ``FakeChat``), it is returned as-is so
    the whole pipeline can run offline. Otherwise a real ``ChatAnthropic`` is
    constructed lazily (import deferred so this module imports without the
    dependency installed).
    """
    if chat is not None:
        # Injected (tests): still meter so budget gating is exercised; FakeChat
        # carries no usage metadata, so it records zero cost.
        return _MeteredChat(chat, model)

    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": kw.pop("max_tokens", 1500),
    }
    if settings.anthropic_api_key:
        params["api_key"] = settings.anthropic_api_key
    params.update(kw)
    return _MeteredChat(ChatAnthropic(**params), model)
