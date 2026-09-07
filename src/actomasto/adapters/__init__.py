"""Incremental, text-free checkpoints for three explicitly supported clients."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

from .schemas import AdapterError, PARSERS, VERSIONS, digest, text_blocks

MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_TURN_BYTES = 32 * 1024 * 1024


def _repository(cwd, repositories):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    directory = Path(cwd).resolve()
    matches = []
    for repository in repositories:
        for root in repository.get("paths", []):
            path = Path(root).resolve()
            if directory == path or path in directory.parents:
                matches.append((len(path.parts), repository["id"]))
    if not matches:
        return None
    depth = max(item[0] for item in matches)
    identities = {identity for size, identity in matches if size == depth}
    return identities.pop() if len(identities) == 1 else None


def _text(client, row):
    if client == "codex":
        return text_blocks(row["payload"]["content"], {"input_text", "output_text"}, {"input_image", "image"})
    if client == "claude":
        return text_blocks(row["message"]["content"], {"text"}, PARSERS[client].blocks)
    return text_blocks(row["message"]["content"], {"text"}, {"thinking", "toolCall", "image"})


def _read_record(stream, offset, expected=None, cancelled=None):
    if cancelled is not None:
        cancelled()
    position = stream.tell()
    try:
        stream.seek(offset)
        raw = stream.readline(MAX_RECORD_BYTES + 1)
        if expected is not None and digest(raw.hex()) != expected:
            raise AdapterError("source_changed")
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise AdapterError("source_changed") from None
    finally:
        stream.seek(position)


def _finish(turn, client, stream, eligible, cursor_get, cursor_set, now, references, cancelled=None):
    if not turn:
        return None
    identifier = digest(f"{client}:{turn['session']}:{turn['user']}")
    marker = f"adapter-unit:{client}:{identifier}"
    if cursor_get(marker):
        return None
    end = turn["end"]
    start = turn["start"]
    if start is None or end is None or start > end or turn["invalid"] or not turn["repository"]:
        cursor_set(marker, {"reason": "ambiguous_turn"})
        return None
    if end > now:
        return None
    repository = turn["repository"]
    if not eligible(repository, start, end):
        cursor_set(marker, {"reason": "ineligible_interval"})
        return None
    refs = []
    tail = turn["tail"]
    visited = set()
    while tail is not None:
        if tail in visited or tail not in references:
            raise AdapterError("unknown_branch_lineage")
        visited.add(tail)
        ref = references[tail]
        refs.append(ref)
        tail = ref["previous"]
    refs.reverse()
    items = []
    size = 0
    for ref in refs:
        if not eligible(repository, ref["time"], ref["time"]):
            cursor_set(marker, {"reason": "ineligible_message"})
            return None
        row = _read_record(stream, ref["offset"], ref["hash"], cancelled)
        text = _text(client, row)
        size += len(text.encode("utf-8"))
        if size > MAX_TURN_BYTES:
            cursor_set(marker, {"reason": "oversized_source"})
            return None
        if text:
            source_ref = {"client": client, "session_id": turn["session"], "message_ids": [ref["id"]]}
            items.append({"id": digest(f"{client}:{turn['session']}:{ref['id']}"),
                          "text": text, "provenance": "user_reported" if ref["role"] == "user" else "assistant_reported",
                          "event_time": ref["time"], "source_ref": source_ref})
    if not items or items[0]["provenance"] != "user_reported":
        cursor_set(marker, {"reason": "unknown_provenance"})
        return None
    return {"id": identifier, "repository_id": repository, "kind": "conversation",
            "event_time": start, "event_end": end, "equivalent_id": None,
            "source_ref": {"client": client, "session_id": turn["session"],
                           "message_ids": [ref["id"] for ref in refs]},
            "paths": [], "items": items, "adapter": client,
            "adapter_version": VERSIONS[client], "partial_source": False}


def _touch(turn, event, repositories):
    if not turn:
        return
    occurred = event["time"]
    association = _repository(event["cwd"], repositories)
    if occurred is None or association != turn["repository"]:
        turn["invalid"] = True
    elif turn["end"] is not None and occurred < turn["end"]:
        turn["invalid"] = True
    else:
        turn["end"] = occurred
    if event.get("invalid"):
        turn["invalid"] = True
    if "end" in event:
        if event["end"] is None or occurred is None or event["end"] < occurred:
            turn["invalid"] = True
        else:
            turn["end"] = event["end"]


def _collect_stream(client, path, repositories, cursor_get, cursor_set, eligible, cancelled=None):
    def checkpoint(key, value):
        if cancelled is not None:
            cancelled()
        cursor_set(key, value)

    if cancelled is not None:
        cancelled()
    parser = PARSERS[client]()
    key = f"adapter-stream:{client}:{digest(str(path.resolve()))}"
    previous = cursor_get(key) or {}
    now = time.time()
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        identity = [stat.st_dev, stat.st_ino]
        valid = previous.get("identity") == identity and previous.get("offset", 0) <= stat.st_size and previous.get("version") == VERSIONS[client]
        if valid and previous.get("anchor"):
            anchor = previous["anchor"]
            if cancelled is not None:
                cancelled()
            stream.seek(anchor["offset"])
            valid = digest(stream.read(anchor["length"]).hex()) == anchor["hash"]
        if valid and previous.get("quarantine"):
            return
        state = copy.deepcopy(previous) if valid else {
            "identity": identity, "version": VERSIONS[client], "offset": 0,
            "context": {}, "nodes": {}, "linear": None, "candidate": None,
            "record_index": 0, "references": {},
        }
        stream.seek(state["offset"])
        while True:
            offset = stream.tell()
            if cancelled is not None:
                cancelled()
            raw = stream.readline(MAX_RECORD_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_RECORD_BYTES:
                state.update(quarantine="oversized_source")
                break
            if not raw.endswith(b"\n"):
                state["incomplete_write"] = True
                break
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                state["quarantine"] = "malformed_record"
                break
            context = copy.deepcopy(state["context"])
            try:
                event = parser.parse(row, context, offset)
            except AdapterError as exc:
                state["error"] = exc.code
                checkpoint(key, state)
                raise
            # A future record remains unread at the durable checkpoint. Both
            # message start and verified completion times must have occurred.
            if any(value is not None and value > now for value in (event["time"], event.get("end"))):
                state["deferred_future"] = True
                break
            state["context"] = context
            session = event["session"] or context.get("session")
            event["session"] = session
            native = row.get("uuid", row.get("id"))
            if client == "codex":
                native = row["payload"].get("id") if row["type"] == "response_item" else native
            if native is None:
                native = digest(f"{session}:{context.get('turn', '')}:{state.get('record_index', 0)}:{digest(raw.hex())}")
            state["record_index"] = state.get("record_index", 0) + 1
            event["id"] = str(native)
            event["hash"] = digest(raw.hex())
            node_key = f"{session}:{native}"
            if parser.linked:
                parent = state["nodes"].get(f"{session}:{event['parent']}")
            else:
                parent = state["linear"]
            turn = copy.deepcopy(parent)
            role = event["role"]
            if role == "candidate":
                candidate = {k: v for k, v in event.items() if k != "text"}
                state["candidate"] = candidate
            elif role == "confirm":
                candidate = state.get("candidate")
                state["candidate"] = None
                if candidate and candidate["confirmation"] == event["confirmation"]:
                    candidate["text"] = _text(client, _read_record(stream, candidate["offset"], candidate["hash"], cancelled))
                    candidate["role"] = "user"
                    event = candidate
                    role = "user"
                else:
                    role = "reset"
            emitted = []
            if role == "user":
                if turn:
                    _touch(turn, event, repositories)
                    unit = _finish(turn, client, stream, eligible, cursor_get, checkpoint, now, state["references"], cancelled)
                    if unit:
                        emitted.append(unit)
                turn = {"user": event["id"], "session": session, "tail": None,
                        "repository": _repository(event["cwd"], repositories),
                        "start": event["time"], "end": event["time"],
                        "invalid": not bool(event["text"])}
            elif role == "reset":
                turn = None
            if role in {"user", "assistant", "boundary"}:
                _touch(turn, event, repositories)
            # Even excluded tool/progress records carry project/time boundaries.
            elif role == "metadata" and turn and (event["time"] is not None or row.get("type") in {"user", "assistant", "message", "response_item"}):
                _touch(turn, event, repositories)
            if role in {"user", "assistant"} and turn:
                ref_key = f"{session}:{event['id']}"
                if ref_key not in state["references"]:
                    state["references"][ref_key] = {
                        "offset": event["offset"], "id": event["id"], "hash": event["hash"],
                        "time": event["time"], "role": role, "previous": turn["tail"],
                    }
                turn["tail"] = ref_key
            if event["complete"]:
                unit = _finish(turn, client, stream, eligible, cursor_get, checkpoint, now, state["references"], cancelled)
                if unit:
                    emitted.append(unit)
                turn = None
            if parser.linked:
                if native is not None:
                    state["nodes"][node_key] = turn
            else:
                state["linear"] = turn
            state.update(offset=stream.tell(), anchor={"offset": offset, "length": len(raw), "hash": digest(raw.hex())})
            state.pop("incomplete_write", None)
            state.pop("deferred_future", None)
            state.pop("error", None)
            # Persist only after the consumer has processed each yielded unit.
            # Crash before enqueue/checkpoint => repeat the stable unit, never
            # lose it. Global text-free unit markers survive rotation and clones.
            for unit in emitted:
                yield unit
                checkpoint(f"adapter-unit:{client}:{unit['id']}", {"reason": "collected"})
            if emitted:
                checkpoint(key, state)
        if parser.linked:
            active_users = {(turn["session"], turn["user"]) for turn in state["nodes"].values() if turn}
            state["pending_turns"] = sum(
                not cursor_get("adapter-unit:" + client + ":" + digest(f"{client}:{session}:{user}"))
                for session, user in active_users
            )
        else:
            state["pending_turns"] = int(bool(state["linear"]))
        checkpoint(key, state)


def collect(client: str, root: Path, repositories: list[dict], cursor_get, cursor_set, eligible, cancelled=None):
    """Yield complete eligible turns; never persist unfiltered transcript text.

    A malformed complete stream is quarantined in its cursor, while independent
    streams continue. Unsupported schema raises AdapterError for this client.
    Missing roots are empty sources; unreadable configured roots are failures.
    The optional cancellation callback raises before reads and durable checkpoints.
    """
    if client not in PARSERS:
        raise AdapterError("unsupported_client")
    root = Path(root).expanduser()
    if cancelled is not None:
        cancelled()
    if not root.exists():
        return
    if not root.is_dir():
        raise AdapterError("invalid_source_root")
    try:
        for path in sorted(root.rglob("*.jsonl")):
            if cancelled is not None:
                cancelled()
            if path.is_symlink() or not path.is_file():
                continue
            yield from _collect_stream(client, path, repositories, cursor_get, cursor_set, eligible, cancelled)
    except OSError:
        raise AdapterError("source_unavailable") from None


__all__ = ["AdapterError", "collect"]
