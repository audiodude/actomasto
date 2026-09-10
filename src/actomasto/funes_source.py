"""Read-only Funes protocol consumer; authorization stays outside harness parsing."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

VERSIONS = {"claude": "claude-schema2", "codex": "codex-0.144.1-schema1", "omp": "omp-session3-schema1"}
MAX_TURN_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_STREAM_STATUS = {"complete", "incomplete_turn", "incomplete_write", "deferred_future",
                  "malformed_record", "oversized_source"}


class SourceError(Exception):
    """A content-free dependency or harness failure."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "invalid_response"
        super().__init__(self.code)


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _time(value):
    return type(value) in (int, float) and math.isfinite(value)


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


class FunesSource:
    def __init__(self, config, cancelled=None):
        if not isinstance(config, dict) or set(config) != {"executable", "corpus", "scope"}:
            raise SourceError("invalid_configuration")
        if any(not isinstance(value, str) or not Path(value).is_absolute() or "\x00" in value
               for value in config.values()):
            raise SourceError("invalid_configuration")
        self.config = dict(config)
        self.cancelled = cancelled
        self.status = {"dependency": "unchecked", "protocol": "unchecked"}
        self.supported = {}

    def _check(self):
        if self.cancelled is not None:
            self.cancelled()

    def request(self, op, **fields):
        """Drain bounded pipes while checking cancellation; never retain diagnostics."""
        if op not in {"capabilities", "enumerate", "turns", "read"}:
            raise SourceError("read_only_interface")
        self._check()
        request = json.dumps({"protocol": 1, "op": op, "corpus": self.config["corpus"],
                              "scope": self.config["scope"], **fields}, separators=(",", ":")).encode()
        if len(request) > 65536:
            raise SourceError("oversized_request")
        output = bytearray()
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
        try:
            with subprocess.Popen([self.config["executable"], "source"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        for pipe in (process.stdin, process.stdout):
                            os.set_blocking(pipe.fileno(), False)
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                        selector.register(process.stdout, selectors.EVENT_READ)
                        position = 0
                        deadline = time.monotonic() + 120
                        while selector.get_map():
                            self._check()
                            if time.monotonic() >= deadline:
                                raise SourceError("dependency_timeout")
                            for key, _ in selector.select(.1):
                                self._check()
                                if key.fileobj is process.stdin:
                                    try:
                                        position += os.write(key.fd, request[position:position + 65536])
                                    except BrokenPipeError:
                                        position = len(request)
                                    if position == len(request):
                                        selector.unregister(key.fileobj)
                                        process.stdin.close()
                                else:
                                    chunk = os.read(key.fd, min(65536, MAX_RESPONSE_BYTES + 1 - len(output)))
                                    if not chunk:
                                        selector.unregister(key.fileobj)
                                    else:
                                        output.extend(chunk)
                                        if len(output) > MAX_RESPONSE_BYTES:
                                            raise SourceError("oversized_response")
                        code = process.wait(timeout=max(.01, deadline - time.monotonic()))
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
        except OSError:
            self.status["dependency"] = "unavailable"
            raise SourceError("dependency_unavailable") from None
        except subprocess.TimeoutExpired:
            raise SourceError("dependency_timeout") from None
        self._check()
        try:
            response = json.loads(output)
        except (ValueError, UnicodeDecodeError):
            raise SourceError("invalid_response") from None
        if (not isinstance(response, dict) or type(response.get("protocol")) is not int
                or response["protocol"] != 1 or type(response.get("ok")) is not bool):
            raise SourceError("unsupported_protocol")
        if not response["ok"]:
            error = response.get("error")
            raise SourceError(error.get("code") if isinstance(error, dict) else "invalid_response")
        if code or not isinstance(response.get("result"), dict):
            raise SourceError("invalid_response")
        self.status["dependency"] = "available"
        return response["result"]

    def capabilities(self):
        result = self.request("capabilities")
        if (type(result.get("protocol")) is not int or result["protocol"] != 1 or result.get("identity") != "actomasto-v1"
                or not isinstance(result.get("build_revision"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", result["build_revision"])
                or any(result.get(flag) is not True for flag in
                       ("local_only", "metadata_only", "revision_bound", "snapshot_enumeration", "coverage_freshness"))
                or not isinstance(result.get("harnesses"), dict)):
            self.status["protocol"] = "incompatible"
            raise SourceError("unsupported_protocol")
        self.supported = result["harnesses"]
        self.status.update(protocol="compatible", build_revision=result["build_revision"])
        return result

    def _turn(self, value, client, *, content=False):
        """Validate identity and complete membership before policy or persistence."""
        if not isinstance(value, dict):
            raise SourceError("invalid_response")
        required = {"ordinal", "id", "session_id", "user_id", "start", "end", "invalid",
                    "boundaries", "items", "message_ids", "bytes"}
        if not required <= value.keys():
            raise SourceError("invalid_response")
        if (type(value["ordinal"]) is not int or value["ordinal"] < 0
                or type(value["bytes"]) is not int or value["bytes"] < 0
                or type(value["invalid"]) is not bool
                or not all(isinstance(value[k], str) for k in ("id", "user_id"))
                or value["session_id"] is not None and not isinstance(value["session_id"], str)
                or not value["session_id"] and not value["invalid"]
                or value["id"] != _digest(f"{client}:{value['session_id']}:{value['user_id']}")
                or any(value[k] is not None and not _time(value[k]) for k in ("start", "end"))
                or not isinstance(value["boundaries"], list) or not value["boundaries"]
                or not isinstance(value["items"], list) or not isinstance(value["message_ids"], list)):
            raise SourceError("invalid_response")
        for boundary in value["boundaries"]:
            if (not isinstance(boundary, dict) or not {"cwd", "time", "end", "invalid"} <= boundary.keys()
                    or type(boundary["invalid"]) is not bool
                    or boundary["cwd"] is not None and not isinstance(boundary["cwd"], str)
                    or any(boundary[k] is not None and not _time(boundary[k]) for k in ("time", "end"))):
                raise SourceError("invalid_response")
        message_ids = []
        size = 0
        for item in value["items"]:
            if (not isinstance(item, dict) or not {"id", "message_id", "role", "time"} <= item.keys()
                    or not isinstance(item["message_id"], str) or item["role"] not in ("user", "assistant")
                    or item["id"] != _digest(f"{client}:{value['session_id']}:{item['message_id']}")
                    or item["time"] is not None and not _time(item["time"])):
                raise SourceError("invalid_response")
            identity = item.get("identity")
            if (not isinstance(identity, dict) or set(identity) != {"native_id", "record_index", "turn_context", "raw_record_digest"}
                    or type(identity["record_index"]) is not int or identity["record_index"] < 0
                    or not isinstance(identity["turn_context"], str)
                    or not isinstance(identity["raw_record_digest"], str) or not _HEX.fullmatch(identity["raw_record_digest"])):
                raise SourceError("incompatible_identity")
            expected = (str(identity["native_id"]) if identity["native_id"] is not None else
                        _digest(f"{value['session_id']}:{identity['turn_context']}:{identity['record_index']}:{identity['raw_record_digest']}"))
            if expected != item["message_id"]:
                raise SourceError("incompatible_identity")
            if content:
                if not isinstance(item.get("text"), str):
                    raise SourceError("invalid_response")
                size += len(item["text"].encode())
            elif "text" in item:
                raise SourceError("metadata_contains_content")
            message_ids.append(item["message_id"])
        if message_ids != value["message_ids"] or len(set(message_ids)) != len(message_ids):
            raise SourceError("invalid_response")
        if content and (size != value["bytes"] or size > MAX_TURN_BYTES):
            raise SourceError("oversized_source" if size > MAX_TURN_BYTES else "invalid_response")
        return value

    def health(self, client):
        """Metadata-only health check; never mark, enqueue, or read turn text."""
        if self.supported.get(client) != VERSIONS.get(client):
            raise SourceError("incompatible_harness")
        cursor = None
        visited = set()
        self.status.update(coverage="unchecked", pending_turns=0, streams={})
        while True:
            page = self.request("enumerate", harness=client, cursor=cursor, limit=64)
            if (not isinstance(page.get("sources"), list) or page.get("coverage") not in {"current", "lagging", "unavailable"}
                    or not _time(page.get("refreshed_at")) or not _time(page.get("lag_seconds"))):
                raise SourceError("invalid_response")
            self.status.update(coverage=page["coverage"], refreshed_at=page["refreshed_at"], lag_seconds=page["lag_seconds"])
            if page["coverage"] != "current":
                raise SourceError("coverage_unavailable")
            for source in page["sources"]:
                if not isinstance(source, dict) or source.get("harness") != client:
                    raise SourceError("invalid_response")
                if source.get("state") == "missing":
                    raise SourceError("source_missing")
                if source.get("state") != "present" or not all(isinstance(source.get(k), str) for k in ("id", "revision")):
                    raise SourceError("invalid_response")
                result = self.request("turns", source_id=source["id"], revision=source["revision"], limit=1)
                status = result.get("status")
                if status not in _STREAM_STATUS:
                    raise SourceError(status)
                if type(result.get("pending_turns")) is not int or result["pending_turns"] < 0:
                    raise SourceError("invalid_response")
                self.status["pending_turns"] += result["pending_turns"]
                self.status["streams"][status] = self.status["streams"].get(status, 0) + 1
            cursor = page.get("next_cursor")
            if cursor is None:
                return
            if not isinstance(cursor, str) or cursor in visited:
                raise SourceError("invalid_cursor")
            visited.add(cursor)

    def collect(self, client, repositories, cursor_get, cursor_set, eligible, seen=None):
        if not self.supported:
            self.capabilities()
        if self.supported.get(client) != VERSIONS.get(client):
            raise SourceError("incompatible_harness")
        checkpoint = "funes-enumeration:" + client
        saved = cursor_get(checkpoint) or {}
        cursor = saved.get("cursor")
        recovered = False
        self.status.update(coverage="unchecked", pending_turns=0, streams={})
        visited = set()
        while True:
            self._check()
            try:
                page = self.request("enumerate", harness=client, cursor=cursor, limit=64)
            except SourceError as exc:
                if exc.code != "invalid_cursor" or recovered:
                    raise
                cursor = None
                recovered = True
                continue
            if (not isinstance(page.get("sources"), list) or len(page["sources"]) > 64
                    or not isinstance(page.get("snapshot"), str) or not isinstance(page.get("scope_id"), str)
                    or page.get("coverage") not in {"current", "lagging", "unavailable"}
                    or not _time(page.get("refreshed_at")) or not _time(page.get("lag_seconds"))
                    or page.get("next_cursor") is not None and not isinstance(page["next_cursor"], str)):
                raise SourceError("invalid_response")
            self.status.update(coverage=page["coverage"], refreshed_at=page["refreshed_at"], lag_seconds=page["lag_seconds"])
            if page["coverage"] != "current":
                raise SourceError("coverage_unavailable")
            for source in page["sources"]:
                if (not isinstance(source, dict) or source.get("harness") != client
                        or not isinstance(source.get("id"), str) or not _HEX.fullmatch(source["id"])
                        or source.get("state") not in {"present", "missing"}
                        or not isinstance(source.get("revision"), str)):
                    raise SourceError("invalid_response")
                if source["state"] == "missing":
                    self.status["streams"]["source_missing"] = self.status["streams"].get("source_missing", 0) + 1
                    continue
                offset = 0
                while True:
                    turns = self.request("turns", source_id=source["id"], revision=source["revision"], offset=offset, limit=64)
                    status = turns.get("status")
                    if not isinstance(status, str) or status not in _STREAM_STATUS:
                        raise SourceError(status)
                    if (not isinstance(turns.get("turns"), list) or len(turns["turns"]) > 64
                            or type(turns.get("pending_turns")) is not int or turns["pending_turns"] < 0):
                        raise SourceError("invalid_response")
                    for raw_turn in turns["turns"]:
                        turn = self._turn(raw_turn, client)
                        marker = f"adapter-unit:{client}:{turn['id']}"
                        self._check()
                        if cursor_get(marker):
                            continue
                        reason = None
                        start, end = turn["start"], turn["end"]
                        associations = {_repository(boundary["cwd"], repositories) for boundary in turn["boundaries"]}
                        repository = next(iter(associations)) if len(associations) == 1 else None
                        if (turn["invalid"] or start is None or end is None or start > end or not repository
                                or any(boundary["invalid"] or boundary["time"] is None for boundary in turn["boundaries"])):
                            reason = "ambiguous_turn"
                        elif end > time.time():
                            continue
                        elif not eligible(repository, start, end):
                            reason = "ineligible_interval"
                        elif any(item["time"] is None or not eligible(repository, item["time"], item["time"])
                                 for item in turn["items"]):
                            reason = "ineligible_message"
                        elif turn["bytes"] > MAX_TURN_BYTES:
                            reason = "oversized_source"
                        elif not turn["items"] or turn["items"][0]["role"] != "user":
                            reason = "unknown_provenance"
                        if reason:
                            self._check()
                            cursor_set(marker, {"reason": reason})
                            continue
                        unit = {"id": turn["id"], "repository_id": repository, "kind": "conversation",
                                "event_time": start, "event_end": end, "equivalent_id": None,
                                "source_ref": {"client": client, "session_id": turn["session_id"],
                                               "message_ids": turn["message_ids"]},
                                "paths": [], "adapter": client, "adapter_version": VERSIONS[client], "partial_source": False}
                        if seen is None or not seen(unit):
                            result = self.request("read", source_id=source["id"], revision=source["revision"], ordinal=turn["ordinal"])
                            original = self._turn(result.get("turn"), client, content=True)
                            metadata = {**original, "items": [{k: v for k, v in item.items() if k != "text"} for item in original["items"]]}
                            if metadata != turn:
                                raise SourceError("source_changed")
                            unit["items"] = [{"id": item["id"], "text": item["text"],
                                              "provenance": "user_reported" if item["role"] == "user" else "assistant_reported",
                                              "event_time": item["time"],
                                              "source_ref": {"client": client, "session_id": turn["session_id"],
                                                             "message_ids": [item["message_id"]]}}
                                             for item in original["items"] if item["text"]]
                            if not unit["items"] or unit["items"][0]["provenance"] != "user_reported":
                                self._check()
                                cursor_set(marker, {"reason": "unknown_provenance"})
                                continue
                            self._check()
                            yield unit
                        self._check()
                        cursor_set(marker, {"reason": "collected"})
                    next_offset = turns.get("next_offset")
                    if next_offset is None:
                        self.status["pending_turns"] += turns["pending_turns"]
                        self.status["streams"][status] = self.status["streams"].get(status, 0) + 1
                        break
                    if type(next_offset) is not int or next_offset <= offset:
                        raise SourceError("invalid_response")
                    offset = next_offset
            cursor = page["next_cursor"]
            self._check()
            cursor_set(checkpoint, {"cursor": cursor, "snapshot": page["snapshot"], "scope_id": page["scope_id"]})
            if cursor is None:
                if self.status["streams"].get("source_missing"):
                    raise SourceError("source_missing")
                return
            if cursor in visited:
                raise SourceError("invalid_cursor")
            visited.add(cursor)
