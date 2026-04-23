"""Agent loop supporting two tool-use modes side-by-side:

  AgentMode.NATIVE — use Ollama's built-in tool_calls protocol.
  AgentMode.JSON   — prompt the model for strict JSON; parse and dispatch.

Both modes share the same tool registry and the same Tier-1/2 semantics. The
mode is a per-run choice so we can A/B them with the same workload.
"""

from __future__ import annotations

import enum
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from hermes.llm import LLMResponse, llm
from hermes.tools import (
    Tool,
    all_tools,
    enqueue_for_approval,
    get_tool,
    log_event,
)

log = logging.getLogger(__name__)


class AgentMode(str, enum.Enum):
    NATIVE = "native"
    JSON = "json"


class HaltReason(str, enum.Enum):
    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"
    MAX_ITERS = "max_iters"
    PARSE_ERROR = "parse_error"
    LLM_ERROR = "llm_error"


@dataclass
class AgentRun:
    session_id: str
    mode: AgentMode
    final_text: str = ""
    halt_reason: HaltReason = HaltReason.COMPLETED
    approvals_queued: list[int] = field(default_factory=list)
    llm_sources: list[str] = field(default_factory=list)
    iterations: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


SYSTEM_PROMPT_BASE = """You are an intake assistant for Oak & Partners, a small legal firm. \
Your job is to qualify prospective clients, record intake details, and draft follow-up \
communications and invoices. Be concise and professional.

Rules you must follow:
- NEVER provide legal advice. If asked, offer a consultation instead.
- Confirm before creating new client records if a similar client already exists.
- Keep tool arguments minimal and well-formed.
- When you have everything you need, stop calling tools and give a final response.

Tier-2 tools (send_email, create_invoice) will be queued for human approval. \
Call them when appropriate; do not wait for confirmation — the system handles queuing.
"""


def build_json_system_prompt(tools: list[Tool]) -> str:
    tools_block = "\n".join(t.to_prompt_block() for t in tools)
    return (
        SYSTEM_PROMPT_BASE
        + "\n\nTools available:\n"
        + tools_block
        + "\n\nOUTPUT FORMAT — respond with ONLY a single JSON object, no prose:\n"
        + "When calling tools:\n"
        + '  {"thought": "<brief reasoning>", "tool_calls": ['
        + '{"name": "<tool_name>", "arguments": {...}}]}\n'
        + "When you have a final answer:\n"
        + '  {"thought": "<brief reasoning>", "final_response": "<text for the user>"}\n'
        + "Never include both tool_calls and final_response. Never include markdown fences."
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Be tolerant: strip code fences, grab the outermost {...} if needed."""
    s = text.strip()
    if s.startswith("```"):
        # trim ```json ... ``` fences
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(s)
        if not m:
            raise
        return json.loads(m.group(0))


def run_agent(
    user_input: str,
    *,
    mode: AgentMode = AgentMode.NATIVE,
    max_iters: int = 8,
    session_id: str | None = None,
    extra_context: str | None = None,
    halt_on_tier2: bool = False,
) -> AgentRun:
    session_id = session_id or uuid.uuid4().hex[:12]
    tools = all_tools()
    run = AgentRun(session_id=session_id, mode=mode)

    if mode == AgentMode.JSON:
        system_prompt = build_json_system_prompt(tools)
    else:
        system_prompt = SYSTEM_PROMPT_BASE

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if extra_context:
        messages.append({"role": "system", "content": f"Context:\n{extra_context}"})
    messages.append({"role": "user", "content": user_input})

    log_event(session_id, "agent_start", note=f"mode={mode.value}; input={user_input[:200]}")

    for i in range(max_iters):
        run.iterations = i + 1
        try:
            resp = _call_llm(mode, messages, tools)
        except Exception as e:
            log_event(session_id, "llm_error", note=str(e))
            run.halt_reason = HaltReason.LLM_ERROR
            run.final_text = f"LLM error: {e}"
            break

        run.llm_sources.append(resp.source)

        # Extract tool calls + possible final text
        tool_calls, final_text, parse_error = _parse_response(mode, resp)
        if parse_error:
            # Retry once on malformed JSON by nudging the model.
            log_event(session_id, "parse_error", note=parse_error)
            if mode == AgentMode.JSON and i < max_iters - 1:
                messages.append({"role": "assistant", "content": resp.text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON. "
                            "Respond again with ONLY a single JSON object matching the required schema."
                        ),
                    }
                )
                continue
            run.halt_reason = HaltReason.PARSE_ERROR
            run.final_text = resp.text
            break

        # No tool calls → final
        if not tool_calls:
            run.final_text = final_text or resp.text
            run.halt_reason = HaltReason.COMPLETED
            log_event(session_id, "agent_final", llm_source=resp.source, note=run.final_text[:500])
            break

        # Execute / enqueue each tool call in order
        halt = False
        for tc in tool_calls:
            tool_name = tc.get("name")
            tool_args = tc.get("arguments") or {}
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except json.JSONDecodeError:
                    tool_args = {"_raw": tool_args}

            tool = get_tool(tool_name or "")
            if not tool:
                tool_msg = {"ok": False, "error": f"unknown tool: {tool_name}"}
                log_event(
                    session_id,
                    "tool_unknown",
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_result=tool_msg,
                    llm_source=resp.source,
                )
                _append_tool_turn(messages, mode, resp, tc, tool_msg)
                continue

            if tool.tier == 3:
                run.halt_reason = HaltReason.BLOCKED
                run.final_text = f"Blocked: {tool.name} is not permitted."
                log_event(
                    session_id,
                    "tool_blocked",
                    tool_name=tool.name,
                    tool_args=tool_args,
                    llm_source=resp.source,
                )
                halt = True
                break

            if tool.tier == 2:
                req_id = enqueue_for_approval(
                    session_id=session_id,
                    tool_name=tool.name,
                    tool_args=tool_args,
                    rationale=final_text or None,
                )
                run.approvals_queued.append(req_id)
                log_event(
                    session_id,
                    "tier2_queued",
                    tool_name=tool.name,
                    tool_args=tool_args,
                    llm_source=resp.source,
                    note=f"approval_id={req_id}",
                )
                # Feed a "queued" result back so the agent knows the call was
                # recorded and can move on to other tool calls.
                queued_result = {
                    "ok": True,
                    "data": {
                        "status": "queued_for_approval",
                        "approval_id": req_id,
                        "note": (
                            "Action recorded and will execute after human "
                            "approval. Continue with remaining steps."
                        ),
                    },
                }
                _append_tool_turn(messages, mode, resp, tc, queued_result)
                if halt_on_tier2:
                    run.halt_reason = HaltReason.APPROVAL_REQUIRED
                    run.final_text = (
                        f"Queued {tool.name} for approval (request #{req_id}). "
                        + (final_text or "")
                    ).strip()
                    halt = True
                    break
                continue

            # Tier 1: execute inline
            result = tool.execute(tool_args, session_id)
            log_event(
                session_id,
                "tool_called",
                tool_name=tool.name,
                tool_args=tool_args,
                tool_result={"ok": result.ok, "data": result.data, "error": result.error},
                llm_source=resp.source,
            )
            _append_tool_turn(messages, mode, resp, tc, {"ok": result.ok, "data": result.data, "error": result.error})

        if halt:
            break
    else:
        run.halt_reason = HaltReason.MAX_ITERS
        run.final_text = "Max iterations reached without a final response."

    run.messages = messages
    return run


def _call_llm(
    mode: AgentMode,
    messages: list[dict[str, Any]],
    tools: list[Tool],
) -> LLMResponse:
    if mode == AgentMode.NATIVE:
        return llm.chat_with_tools_native(messages, [t.to_ollama_spec() for t in tools])
    return llm.chat(messages, format_json=True)


def _parse_response(
    mode: AgentMode,
    resp: LLMResponse,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Return (tool_calls, final_text, parse_error)."""
    if mode == AgentMode.NATIVE:
        return resp.tool_calls, resp.text, None

    # JSON mode
    try:
        parsed = _extract_json(resp.text)
    except json.JSONDecodeError as e:
        return [], resp.text, f"could not parse JSON: {e}"

    tool_calls = parsed.get("tool_calls") or []
    final_text = parsed.get("final_response") or parsed.get("thought") or ""
    # Only treat tool_calls list as authoritative; ignore final_response if both present.
    if tool_calls:
        return tool_calls, "", None
    return [], parsed.get("final_response", "") or parsed.get("thought", ""), None


def _append_tool_turn(
    messages: list[dict[str, Any]],
    mode: AgentMode,
    resp: LLMResponse,
    tool_call: dict[str, Any],
    tool_result: dict[str, Any],
) -> None:
    """Record assistant's tool call + the tool's result in the conversation history."""
    if mode == AgentMode.NATIVE:
        messages.append(
            {
                "role": "assistant",
                "content": resp.text or "",
                "tool_calls": [
                    {"function": {"name": tool_call["name"], "arguments": tool_call.get("arguments") or {}}}
                ],
            }
        )
        messages.append(
            {"role": "tool", "name": tool_call["name"], "content": json.dumps(tool_result, default=str)}
        )
    else:
        # JSON mode — append as plain assistant/user turns, keeps the model's expected shape.
        messages.append({"role": "assistant", "content": resp.text})
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"tool_result": {"name": tool_call["name"], "result": tool_result}},
                    default=str,
                ),
            }
        )
