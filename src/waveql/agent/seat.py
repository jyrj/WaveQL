"""A model seat: one vendor's chat + tool-calling loop, instrumented.

What matters here is not that it calls a model, but that it produces the numbers
the ablation is scored on. Every episode records:

* the four-class token counts (prompt, output, thought, cached) per turn, so
  dollars-per-fix is computed from what was actually billed rather than
  estimated from string lengths;
* every tool call and its result size, so "queries per fix" is measured;
* the full transcript, so a claimed fix can be audited after the fact.

The seat deliberately does NOT know which arm it is. Arms differ only in the tool
list handed to :meth:`run`, because the tooling is the independent variable and
everything else -- prompt, budget, model, temperature -- is held constant.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


@dataclass
class Usage:
    """Four-class token accounting, summed over an episode."""

    prompt: int = 0
    output: int = 0
    thought: int = 0
    cached: int = 0
    calls: int = 0

    def add(self, meta: Any) -> None:
        if meta is None:
            return
        self.calls += 1
        self.prompt += getattr(meta, "prompt_token_count", 0) or 0
        self.output += getattr(meta, "candidates_token_count", 0) or 0
        self.thought += getattr(meta, "thoughts_token_count", 0) or 0
        self.cached += getattr(meta, "cached_content_token_count", 0) or 0

    @property
    def total(self) -> int:
        return self.prompt + self.output + self.thought


@dataclass
class ToolCall:
    turn: int
    name: str
    args: dict
    result_chars: int
    truncated: bool
    ms: float


@dataclass
class Episode:
    """One agent attempt at one task, with everything needed to score or audit it."""

    task_id: str
    arm: str
    model: str
    seed: int
    answer: str = ""
    turns: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    wall_seconds: float = 0.0
    stop_reason: str = ""
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def queries(self) -> int:
        return len(self.tool_calls)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)

    def write(self, path: str | os.PathLike[str]) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())
        return p


class VertexSeat:
    """Gemini on Vertex AI.

    The model id is never defaulted silently: an unavailable model must fail at
    construction, not three hours into a sweep.
    """

    def __init__(self, model: str, project: str | None = None,
                 location: str | None = None, temperature: float = 0.0,
                 timeout_s: float = 900.0, retries: int = 6):
        from google import genai
        from google.genai import types

        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        if not self.project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is unset; source configs/gcp.env")
        self.model = model
        self.temperature = temperature
        self.retries = retries
        # A REQUEST TIMEOUT IS NOT OPTIONAL. Without one the client blocks on a
        # socket read forever: an ablation run sat for 19 minutes having consumed
        # 4 seconds of CPU, twice, silently stalling the whole experiment. The
        # value is in milliseconds.
        self.client = genai.Client(
            vertexai=True, project=self.project, location=self.location,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )

    def run(
        self,
        *,
        task_id: str,
        arm: str,
        system: str,
        prompt: str,
        declarations: Sequence[dict],
        dispatch: Callable[[str, dict], str],
        max_turns: int = 30,
        max_tokens: int | None = None,
        deadline_s: float = 300.0,
        final_nudge: str | None = None,
        mid_nudge: str | None = None,
        commit_tools: Sequence[str] | None = None,
        seed: int = 0,
    ) -> Episode:
        """Drive one episode to a final text answer or a budget stop."""
        from google.genai import types

        ep = Episode(task_id=task_id, arm=arm, model=self.model, seed=seed)
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=list(declarations))] if declarations else None,
        )
        contents: list[Any] = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        t0 = time.monotonic()
        grace: int | None = None
        reminded = False
        empties = 0
        recovered = False        # tools were handed back after a refused call

        try:
            # The loop bound is the budget PLUS the grace turns: max_turns is
            # when the agent is told to stop investigating, and the extra
            # iterations are what let it act on that. Without them the nudge
            # would land on the last turn and the episode would end unanswered.
            for turn in range(1, max_turns + 4):
                ep.turns = turn
                resp = None
                last: Exception | None = None
                for attempt in range(self.retries):
                    try:
                        resp = self.client.models.generate_content(
                            model=self.model, contents=contents, config=cfg)
                        break
                    except Exception as exc:                       # noqa: BLE001
                        # Retry transport failures, timeouts and QUOTA. An episode
                        # lost to one flaky request is a hole in the experiment's
                        # design matrix, not a result.
                        #
                        # 429 is treated differently from 5xx: a quota refusal
                        # does not clear in seconds, so quota backs off for
                        # minutes while transport errors keep the fast path.
                        last = exc
                        msg = f"{type(exc).__name__}: {exc}"
                        quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                        delay = (min(30 * 2 ** attempt, 240) if quota
                                 else 2 * (attempt + 1))
                        ep.transcript.append(
                            {"turn": turn, "role": "retry", "attempt": attempt + 1,
                             "quota": quota, "sleep": delay, "error": msg[:300]})
                        time.sleep(delay)
                if resp is None:
                    raise last if last else RuntimeError("no response and no error")
                ep.usage.add(getattr(resp, "usage_metadata", None))

                cand = (resp.candidates or [None])[0]
                parts = list(getattr(getattr(cand, "content", None), "parts", None) or [])
                calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

                if not calls:
                    text = (resp.text or "").strip()
                    fr = str(getattr(cand, "finish_reason", "") or "")
                    if not text and empties < 2:
                        # No tool call AND no text is not an answer; it is the
                        # model returning nothing, usually after running out of
                        # output tokens mid-thought. Accepting it would score the
                        # episode zero for a harness reason, so ask again.
                        empties += 1
                        ep.transcript.append({"turn": turn, "role": "recover",
                                              "restored_tools": len(declarations)})
                        ep.transcript.append({"turn": turn, "role": "empty",
                                              "finish_reason": fr, "attempt": empties})
                        hint = ""
                        if "UNEXPECTED_TOOL_CALL" in fr:
                            # Recoverable, not fatal: give every tool back and
                            # ask again. Whatever the model reached for, refusing
                            # it silently is what ends the episode.
                            cfg = types.GenerateContentConfig(
                                system_instruction=system,
                                temperature=self.temperature,
                                tools=[types.Tool(
                                    function_declarations=list(declarations))]
                                if declarations else None)
                            recovered = True
                            # The model reached for a tool that is no longer
                            # declared. Saying so beats asking again and getting
                            # the same empty reply.
                            hint = (" Your last call named a tool that was not "
                                    "available; every tool is available again now.")
                        contents.append(types.Content(role="user", parts=[types.Part(
                            text="Your last message was empty." + hint +
                                 " Give your answer now, briefly, in the required "
                                 "three-line format -- and if you have not yet "
                                 "proposed a fix, propose one first.")]))
                        continue
                    ep.answer = text
                    ep.stop_reason = "answered" if text else f"empty ({fr or 'no reason'})"
                    ep.transcript.append({"turn": turn, "role": "model", "text": ep.answer,
                                          "finish_reason": fr})
                    break

                contents.append(cand.content)
                responses = []
                for fc in calls:
                    args = dict(fc.args or {})
                    t1 = time.monotonic()
                    out = dispatch(fc.name, args)
                    ms = (time.monotonic() - t1) * 1000
                    ep.tool_calls.append(ToolCall(
                        turn=turn, name=fc.name, args=args, result_chars=len(out),
                        truncated="TRUNCATED" in out, ms=ms))
                    ep.transcript.append({"turn": turn, "role": "tool", "name": fc.name,
                                          "args": args, "result": out[:4000]})
                    responses.append(types.Part.from_function_response(
                        name=fc.name, response={"result": out}))
                contents.append(types.Content(role="user", parts=responses))

                if max_tokens and ep.usage.total >= max_tokens:
                    ep.stop_reason = "token budget"
                    break
                # One turn before the budget runs out -- whether that budget is
                # turns or wall-clock -- tell the agent to stop investigating and
                # commit to an answer.
                #
                # The wall-clock half is not redundant: context grows with every
                # tool result, and on a large one a single turn can take many
                # minutes, which a turn count alone does not bound. Both budgets
                # are applied identically to every arm.
                elapsed = time.monotonic() - t0
                if grace is None and (turn >= max_turns or elapsed > deadline_s):
                    # Budget is up. Give the agent a GRACE of two more turns: one
                    # to take its final action with tools still available (for a
                    # repair task that action is proposing the patch), then one
                    # with tools REMOVED so it must answer in text.
                    #
                    # Without the grace, an agent that answers the nudge with one
                    # more tool call would be cut off with an empty answer.
                    if elapsed > deadline_s:
                        ep.stop_reason = "deadline"
                    grace = 2
                    contents.append(types.Content(role="user", parts=[types.Part(
                        text=final_nudge or
                             "You have one turn left. Stop investigating and give "
                             "your final answer now in the required three-line "
                             "format, using your best current hypothesis.")]))
                elif mid_nudge and not reminded and (
                        turn >= max_turns // 2 or elapsed > deadline_s / 2):
                    # Halfway. Ask for a candidate now rather than at the end: an
                    # agent that explores to the last turn and never proposes
                    # scores zero however good its analysis was.
                    reminded = True
                    contents.append(types.Content(role="user", parts=[types.Part(
                        text=mid_nudge)]))
                elif grace is not None:
                    grace -= 1
                    # Tools are NOT withdrawn during the grace turns to force a
                    # commitment: a model that reaches for an undeclared tool
                    # gets FinishReason.UNEXPECTED_TOOL_CALL with no text, which
                    # ends the episode. Nudges without teeth are weaker, but an
                    # agent that ignores advice still gets to answer.
                    if grace <= 0 and not recovered:
                        # Last turn: no tools, so the only possible reply is the
                        # final answer. Skipped once tools have been handed back
                        # after a refused call -- stripping them again would undo
                        # the recovery and re-trigger the very error it recovered
                        # from, one turn later.
                        cfg = types.GenerateContentConfig(
                            system_instruction=system, temperature=self.temperature)
                        contents.append(types.Content(role="user", parts=[types.Part(
                            text="Tools are now closed. Reply with the three "
                                 "required lines and nothing else.")]))
                    if grace < -1:
                        break
            else:
                ep.stop_reason = "turn limit"
        except Exception as exc:                                   # noqa: BLE001
            ep.error = f"{type(exc).__name__}: {exc}"
            ep.stop_reason = "error"

        ep.wall_seconds = time.monotonic() - t0
        return ep


# Vertex list prices, USD per 1M tokens. Committed with the results so a cost
# table is reproducible rather than recomputed against whatever prices are
# current when someone reads the paper.
PRICES: dict[str, tuple[float, ...]] = {
    # model: (input, output[, cached input]) per 1M tokens
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-3.7-flash": (0.30, 2.50),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    # Gemini 3.1 Pro Preview, standard tier, prompts <= 200k tokens; cached input
    # from the Cloud Billing catalog ("Gemini 3.0 / 3.1 Pro Text Input Caching").
    # Above 200k the rates are 4.00 / 18.00 / 0.40, so where a request exceeded
    # 200k tokens of context the figure is a lower bound.
    "gemini-3.1-pro-preview": (2.00, 12.00, 0.20),
}


def cost_usd(model: str, usage: Usage) -> float | None:
    """Dollar cost of an episode, or None when the model is not in the table.

    None rather than zero: an unpriced model must show up as a gap in the cost
    column, not as a free one.
    """
    p = PRICES.get(model.rsplit("/", 1)[-1])
    if p is None:
        return None
    inp, out = p[0], p[1]
    cache = p[2] if len(p) > 2 else inp
    # prompt_token_count INCLUDES the cached tokens (cached_content_token_count
    # is a subset of it), and cached tokens are billed at the cached rate.
    fresh = max(0, usage.prompt - usage.cached)
    return (fresh / 1e6 * inp + usage.cached / 1e6 * cache
            + (usage.output + usage.thought) / 1e6 * out)
