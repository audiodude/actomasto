"""Metadata-inspected local format contracts, not best-effort future parsers.

Inspection (2026-09-06/07), without exporting message text:
* Claude Code observed 2.1.220–2.1.233 releases plus 2.1.260 / 2.1.263
  (exact versions in Claude.versions): type,userType,uuid,parentUuid,cwd,
  sessionId,timestamp,version,message{role,content,stop_reason}; origin.kind
  human and promptSource typed establish user provenance, including sessionKind
  bg. Sidechains, synthetic/tool messages and ambiguous replacements are excluded.
* Codex CLI 0.144.1: session_meta.payload{cwd,id,cli_version,source,
  thread_source}; turn_context.payload{cwd,turn_id}; response_item.payload
  {type:message,role,content:[input_text|output_text],phase,
  internal_chat_message_metadata_passthrough:{turn_id}}. user_message events
  corroborate canonical user text; event copies are never emitted. task_complete
  is the completion marker, not assistant phase or file inactivity.
* Oh My Pi session v3: session{cwd,id,version}; linked message{id,parentId,
  timestamp,message:{role,attribution,content,timestamp,stopReason,completedAt}}.
  Explicit attribution=user establishes provenance; attribution=agent does not.
  Assistant stopReason=stop completes a turn; toolUse/aborted do not.

Unknown content-bearing record/block kinds fail closed. Known non-source record
kinds below are explicitly ignored; their arbitrary payloads are never scanned.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime

VERSIONS = {"claude": "claude-schema2", "codex": "codex-0.144.1-schema1", "omp": "omp-session3-schema1"}


class AdapterError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp(value, *, milliseconds=False):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (float, int)):
            result = float(value) / (1000 if milliseconds else 1)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        else:
            return None
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


# Harness context is not source even when embedded in a genuine user's block.
_INJECTED = re.compile(r"<(system-reminder|system-directive|environment_context|developer_instructions|permissions instructions|turn_aborted|subagent_notification|task-notification|local-command-caveat|local-command-stdout|command-message|command-name|command-args)(?:\s[^>]*)?>.*?</\1>", re.S | re.I)
_FRAMING = re.compile(r"^\s*(?:# AGENTS\.md instructions for |\[Request interrupted by user|\[This is a continuation of a previous conversation|<task-notification>|<subagent_notification>|<environment_context>)", re.I)


def text_blocks(content, allowed: set[str], excluded: set[str]) -> str:
    if isinstance(content, str):
        result = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise AdapterError("unknown_content_schema")
            kind = block["type"]
            if kind in allowed:
                if not isinstance(block.get("text"), str) or set(block) - {"type", "text", "textSignature", "citations"}:
                    raise AdapterError("unknown_content_schema")
                parts.append(block["text"])
            elif kind not in excluded:
                raise AdapterError("unknown_content_schema")
        result = "\n".join(parts)
    else:
        raise AdapterError("unknown_content_schema")
    if _FRAMING.match(result):
        return ""
    result = _INJECTED.sub("", result)
    # An incomplete injection frame has no trustworthy end boundary.
    if re.search(r"<(?:system-reminder|system-directive|environment_context|subagent_notification|task-notification)(?:\s|>)", result, re.I):
        return ""
    return result.strip()


def event(row, context, offset):
    return {"id": str(row.get("uuid", row.get("id", offset))),
            "parent": row.get("parentUuid", row.get("parentId")),
            "session": context.get("session"), "cwd": context.get("cwd"),
            "time": timestamp(row.get("timestamp")), "role": "metadata",
            "text": "", "complete": False, "offset": offset}


class Claude:
    linked = True
    versions = {"2.1.220", "2.1.221", "2.1.223", "2.1.224", "2.1.226", "2.1.227",
                "2.1.228", "2.1.229", "2.1.231", "2.1.232", "2.1.233", "2.1.260", "2.1.263"}
    ignored = {"last-prompt", "mode", "permission-mode", "atis-latch", "attachment", "file-history-snapshot", "ai-title", "queue-operation", "progress", "summary",
               "file-history-delta", "pr-link", "cost-state", "agent-name", "frame-link", "bridge-session", "custom-title"}
    blocks = {"thinking", "redacted_thinking", "tool_use", "tool_result", "image", "document", "server_tool_use", "web_search_tool_result"}
    record_keys = set("apiBlockIndex classifierMetaLines cwd effort entrypoint error gitBranch imagePasteIds isApiErrorMessage isMeta isSidechain message origin parentUuid permissionMode promptId promptSource queueSkipAttachments requestId sessionId session_id sourceToolAssistantUUID timestamp toolUseResult turnCompanion type userType uuid version attributionAgent attributionSkill attributionPlugin attributionMcpServer attributionMcpTool isCompactSummary slug sourceToolUseID mcpMeta toolDenialKind userFeedback sessionKind".split())
    message_keys = set("container content context_management diagnostics id model role stop_details stop_reason stop_sequence type usage".split())

    def parse(self, row, context, offset):
        kind = row.get("type")
        if "cwd" in row:
            context["cwd"] = row["cwd"]
        if kind in self.ignored:
            return event(row, context, offset)
        if kind not in {"user", "assistant", "system"}:
            raise AdapterError("unknown_content_schema")
        if row.get("version") not in self.versions:
            raise AdapterError("unsupported_version")
        required = {"cwd", "uuid", "parentUuid", "sessionId", "isSidechain"}
        if not required <= row.keys():
            raise AdapterError("unknown_content_schema")
        context["session"] = row["sessionId"]
        result = event(row, context, offset)
        result["cwd"] = row["cwd"]
        # These records cannot safely continue the current human-authored turn.
        # In particular, supersession is not verified parent-branch lineage.
        if (row["isSidechain"] or row.get("isCompactSummary") or row.get("attributionAgent")
                or row.get("agentId") or row.get("isVisibleInTranscriptOnly")
                or row.get("isAbortedMidStream") or row.get("supersedesUuids") or row.get("interruptedMessageId")):
            result["role"] = "reset"
            return result
        if kind == "system":
            if row.get("subtype") not in {"away_summary", "local_command", "stop_hook_summary", "turn_duration", "compact_boundary",
                                        "informational", "bridge_status", "model_refusal_fallback"}:
                raise AdapterError("unknown_content_schema")
            if row.get("subtype") in {"compact_boundary", "model_refusal_fallback"}:
                result["role"] = "reset"
            return result
        if set(row) - self.record_keys:
            raise AdapterError("unknown_content_schema")
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role") != kind:
            raise AdapterError("unknown_content_schema")
        if set(message) - self.message_keys:
            raise AdapterError("unknown_content_schema")
        text = text_blocks(message.get("content"), {"text"}, self.blocks)
        if kind == "user":
            origin = row.get("origin")
            human = origin == {"kind": "human"} or (origin is None and row.get("promptSource") == "typed")
            genuine = human and not (row.get("isMeta") or row.get("sourceToolAssistantUUID") or row.get("sourceToolUseID") or row.get("toolUseResult") or row.get("promptSource") == "system")
            if genuine:
                result.update(role="user", text=text)
            elif text and not row.get("isMeta") and not row.get("sourceToolAssistantUUID"):
                result["role"] = "reset"
        elif not row.get("isApiErrorMessage") and not row.get("isMeta"):
            stop = message.get("stop_reason")
            if stop not in {None, "end_turn", "stop_sequence", "tool_use", "max_tokens", "refusal"}:
                raise AdapterError("unknown_content_schema")
            result.update(role="assistant", text=text, complete=stop == "end_turn" and bool(text))
        return result


class Codex:
    linked = False
    ignored_records = {"world_state"}
    ignored_events = {"agent_message", "token_count", "web_search_end", "patch_apply_end", "thread_settings_applied", "agent_reasoning", "exec_command_begin", "exec_command_end", "patch_apply_begin", "warning", "error", "context_compacted"}
    ignored_items = {"reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "web_search_call", "compaction"}
    session_keys = set("base_instructions cli_version context_window cwd git history_mode id model_provider originator session_id source thread_source timestamp".split())

    def parse(self, row, context, offset):
        kind = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise AdapterError("unknown_content_schema")
        if set(row) - {"type", "timestamp", "payload"}:
            raise AdapterError("unknown_content_schema")
        result = event(row, context, offset)
        if kind == "session_meta":
            if payload.get("cli_version") != "0.144.1":
                raise AdapterError("unsupported_version")
            if set(payload) - self.session_keys:
                raise AdapterError("unknown_session_schema")
            context.update(session=payload.get("id"), cwd=payload.get("cwd"), source_ok=payload.get("source") == "cli" and payload.get("thread_source") == "user")
            if not isinstance(context["session"], str) or not isinstance(context["cwd"], str):
                raise AdapterError("unknown_content_schema")
            return result
        if not context.get("session"):
            raise AdapterError("missing_session_header")
        if kind in self.ignored_records:
            return result
        if kind == "turn_context":
            if not isinstance(payload.get("cwd"), str) or not isinstance(payload.get("turn_id"), str):
                raise AdapterError("unknown_content_schema")
            context.update(cwd=payload["cwd"], turn=payload["turn_id"])
            result.update(cwd=context["cwd"], role="boundary")
            return result
        if kind == "event_msg":
            subtype = payload.get("type")
            if subtype == "task_started":
                context["active_turn"] = payload.get("turn_id")
                return result
            if subtype == "user_message":
                if not isinstance(payload.get("message"), str):
                    raise AdapterError("unknown_content_schema")
                result.update(role="confirm", confirmation=digest(payload["message"]))
            elif subtype == "task_complete":
                result.update(role="boundary", complete=True,
                              invalid=payload.get("turn_id") != context.get("active_turn"))
            elif subtype == "turn_aborted":
                result["role"] = "reset"
            elif subtype not in self.ignored_events:
                raise AdapterError("unknown_content_schema")
            return result
        if kind != "response_item":
            raise AdapterError("unknown_content_schema")
        subtype = payload.get("type")
        if subtype in self.ignored_items:
            return result
        if subtype != "message" or payload.get("role") not in {"user", "assistant", "system", "developer"}:
            raise AdapterError("unknown_content_schema")
        if set(payload) - {"type", "role", "content", "id", "phase", "internal_chat_message_metadata_passthrough"}:
            raise AdapterError("unknown_content_schema")
        role = payload["role"]
        if role in {"system", "developer"}:
            return result
        if not isinstance(payload.get("content"), list):
            raise AdapterError("unknown_content_schema")
        text = text_blocks(payload.get("content"), {"input_text", "output_text"}, {"input_image", "image"})
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(metadata, dict) and set(metadata) - {"turn_id"}:
            raise AdapterError("unknown_provenance_schema")
        associated = (isinstance(metadata, dict) and isinstance(metadata.get("turn_id"), str)
                      and metadata["turn_id"] == context.get("turn") == context.get("active_turn"))
        if context.get("source_ok") and associated:
            result.update(role="candidate" if role == "user" else "assistant", text=text)
            if role == "user":
                # Match raw canonical content to the user event before stripping
                # known injected framing. Never emit the duplicate event copy.
                raw = "\n".join(b["text"] for b in payload["content"] if b.get("type") == "input_text")
                result["confirmation"] = digest(raw)
            if role == "assistant" and payload.get("phase") not in {"commentary", "final_answer"}:
                result["role"] = "reset"
        elif role == "user":
            result["role"] = "reset"
        return result


class Omp:
    linked = True
    ignored = {"title", "title_change", "model_change", "thinking_level_change", "custom", "custom_message", "credential_pin", "label", "session_info", "ttsr_injection"}
    message_keys = set("api attribution completedAt content contextSnapshot details duration errorId errorMessage isError model provider providerPayload responseId role steering stopReason timestamp toolCallId toolName ttft usage useless".split())

    def parse(self, row, context, offset):
        kind = row.get("type")
        result = event(row, context, offset)
        if kind == "title":
            return result
        if kind == "session":
            if row.get("version") != 3:
                raise AdapterError("unsupported_version")
            if not isinstance(row.get("id"), str) or not isinstance(row.get("cwd"), str):
                raise AdapterError("unknown_content_schema")
            context.update(session=row["id"], cwd=row["cwd"])
            return result
        if not context.get("session"):
            raise AdapterError("missing_session_header")
        if kind in {"reset_boundary", "compaction", "branch_summary"}:
            result["role"] = "reset"
            return result
        if kind in self.ignored:
            return result
        if kind != "message" or not {"id", "parentId", "message"} <= row.keys():
            raise AdapterError("unknown_content_schema")
        message = row["message"]
        if not isinstance(message, dict):
            raise AdapterError("unknown_content_schema")
        role = message.get("role")
        if role not in {"user", "assistant", "toolResult", "system", "developer"}:
            raise AdapterError("unknown_content_schema")
        result["time"] = timestamp(message.get("timestamp"), milliseconds=True)
        if "cwd" in message or "cwd" in row:
            # Per-message cwd is not part of the inspected v3 message schema.
            raise AdapterError("unknown_project_schema")
        if set(row) - {"type", "id", "parentId", "timestamp", "message"} or set(message) - self.message_keys:
            raise AdapterError("unknown_content_schema")
        if role in {"toolResult", "system", "developer"}:
            return result
        if not isinstance(message.get("content"), list):
            raise AdapterError("unknown_content_schema")
        text = text_blocks(message.get("content"), {"text"}, {"thinking", "toolCall", "image"})
        if role == "user":
            if message.get("attribution") == "user":
                result.update(role="user", text=text)
            else:
                result["role"] = "reset"
        else:
            stop = message.get("stopReason")
            if stop not in {"stop", "toolUse", "aborted", "error", "length"}:
                raise AdapterError("unknown_content_schema")
            result.update(role="assistant", text=text, complete=stop == "stop")
            if stop == "stop":
                result["end"] = timestamp(message.get("completedAt"), milliseconds=True)
        return result


PARSERS = {"claude": Claude, "codex": Codex, "omp": Omp}
