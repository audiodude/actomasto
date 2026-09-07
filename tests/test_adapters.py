"""Sanitized acceptance cases derived from metadata-inspected local schemas."""
import copy
import json
from pathlib import Path

import pytest

from actomasto.adapters import AdapterError, collect
from actomasto.common import SourceCancelled

FIXTURES = Path(__file__).parent / "fixtures"
FILES = {"claude": "claude-2.1.263.jsonl", "codex": "codex-0.144.1.jsonl", "omp": "omp-session3.jsonl"}
START = 1767225600.0


class Source:
    def __init__(self, tmp_path, client):
        self.client = client
        self.root = tmp_path / "sessions"
        self.root.mkdir()
        self.repo = tmp_path / "public"
        self.repo.mkdir()
        self.path = self.root / "session.jsonl"
        self.records = [json.loads(line.replace("/work/public", str(self.repo)))
                        for line in (FIXTURES / FILES[client]).read_text().splitlines()]
        self.cursors = {}
        self.repositories = [{"id": "github.com:1", "paths": [str(self.repo)]}]
        self.write(self.records)

    def write(self, rows, suffix=b""):
        self.path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + suffix)

    def read(self, eligible=lambda repository, start, end: True, cancelled=None):
        return list(collect(self.client, self.root, self.repositories,
                            lambda key: copy.deepcopy(self.cursors.get(key)),
                            lambda key, value: self.cursors.__setitem__(key, copy.deepcopy(value)),
                            eligible, cancelled))

    def status(self):
        return next(value for key, value in self.cursors.items() if key.startswith("adapter-stream:"))


@pytest.mark.parametrize("client", FILES)
def test_supported_formats_only_emit_genuine_text_once(tmp_path, client):
    source = Source(tmp_path, client)
    units = source.read()
    assert len(units) == 1
    unit = units[0]
    assert [item["text"] for item in unit["items"]] == [
        "Explain the cache change.", "I am checking cache invalidation.", "I fixed cache invalidation."]
    assert [item["provenance"] for item in unit["items"]] == ["user_reported", "assistant_reported", "assistant_reported"]
    assert unit["repository_id"] == "github.com:1"
    assert unit["event_time"] == START
    assert unit["event_end"] >= START + 3
    assert str(tmp_path) not in json.dumps(unit)
    assert "cache" not in json.dumps(source.cursors)
    assert source.read() == []
    assert source.status()["pending_turns"] == 0


@pytest.mark.parametrize("client", FILES)
def test_incomplete_tail_survives_restart_then_rotation(tmp_path, client):
    source = Source(tmp_path, client)
    last = json.dumps(source.records[-1]).encode()
    source.write(source.records[:-1], last[:len(last)//2])
    assert source.read() == []
    assert source.status()["incomplete_write"] is True
    assert source.status()["pending_turns"] == 1
    source.cursors = json.loads(json.dumps(source.cursors))
    with source.path.open("ab") as stream:
        stream.write(last[len(last)//2:] + b"\n")
    unit = source.read()[0]
    assert unit["items"][-1]["text"] == "I fixed cache invalidation."
    source.path.rename(source.root / "rotated.jsonl")
    source.write(source.records)
    assert source.read() == []


@pytest.mark.parametrize("client", FILES)
def test_complete_malformed_record_quarantines_only_stream(tmp_path, client):
    source = Source(tmp_path, client)
    source.write(source.records[:-1], b'{"type":bad}\n')
    assert source.read() == []
    assert source.status()["quarantine"] == "malformed_record"
    (source.root / "other.jsonl").write_bytes(
        b"".join(json.dumps(row).encode() + b"\n" for row in source.records))
    assert len(source.read()) == 1


@pytest.mark.parametrize("client", FILES)
def test_unknown_content_pauses_client_without_consuming_record(tmp_path, client):
    source = Source(tmp_path, client)
    rows = copy.deepcopy(source.records)
    index = -2 if client == "codex" else -1
    message = rows[index]["payload"] if client == "codex" else rows[index]["message"]
    message["content"].append({"type": "future_private_payload", "text": "DO_NOT_COLLECT"})
    source.write(rows)
    with pytest.raises(AdapterError, match="unknown_content_schema"):
        source.read()
    assert source.status()["error"] == "unknown_content_schema"
    assert "DO_NOT_COLLECT" not in json.dumps(source.cursors)
    source.write(source.records)
    assert len(source.read()) == 1


@pytest.mark.parametrize("client", FILES)
def test_turn_crossing_excluded_interval_is_not_partially_collected(tmp_path, client):
    source = Source(tmp_path, client)
    def eligible(repository, start, end):
        return not (start < START + 2.5 and end >= START + 1.5)
    assert source.read(eligible) == []
    # An exclusion marker survives relaxed callbacks / restart.
    assert source.read() == []


@pytest.mark.parametrize("client", FILES)
def test_future_completion_does_not_advance_checkpoint(tmp_path, client, monkeypatch):
    source = Source(tmp_path, client)
    monkeypatch.setattr("actomasto.adapters.time.time", lambda: START + 2.5)
    assert source.read() == []
    assert source.status()["deferred_future"] is True
    monkeypatch.setattr("actomasto.adapters.time.time", lambda: START + 10)
    assert len(source.read()) == 1


@pytest.mark.parametrize("client", FILES)
def test_no_idle_timeout_or_phase_based_completion(tmp_path, client):
    source = Source(tmp_path, client)
    rows = copy.deepcopy(source.records)
    if client == "codex":
        rows.pop()
    elif client == "claude":
        rows[-1]["message"]["stop_reason"] = "max_tokens"
    else:
        rows[-1]["message"]["stopReason"] = "aborted"
    source.write(rows)
    assert source.read() == []
    assert source.status()["pending_turns"] == 1


@pytest.mark.parametrize("client", ["claude", "omp"])
def test_explicit_branch_lineage_never_replays_shared_ancestors(tmp_path, client):
    source = Source(tmp_path, client)
    first = source.read()[0]
    user = copy.deepcopy(next(row for row in source.records if row.get("type") == "user" and row.get("origin") or row.get("message", {}).get("attribution") == "user"))
    final = copy.deepcopy(source.records[-1])
    if client == "claude":
        user.update(uuid="u2", parentUuid="u1", timestamp="2026-01-01T00:00:05Z")
        user["message"]["content"] = "Explain the branch change."
        final.update(uuid="a3", parentUuid="u2", timestamp="2026-01-01T00:00:06Z")
    else:
        user.update(id="u2", parentId="u1", timestamp="2026-01-01T00:00:05Z")
        user["message"].update(timestamp=1767225605000, content=[{"type": "text", "text": "Explain the branch change."}])
        final.update(id="a3", parentId="u2", timestamp="2026-01-01T00:00:06Z")
        final["message"].update(timestamp=1767225606000, completedAt=1767225606500)
    source.write(source.records + [user, final])
    second = source.read()
    assert len(second) == 1
    assert second[0]["id"] != first["id"]
    assert [item["text"] for item in second[0]["items"]] == ["Explain the branch change.", "I fixed cache invalidation."]


def test_codex_user_role_without_matching_human_event_is_not_provenance(tmp_path):
    source = Source(tmp_path, "codex")
    rows = [row for row in source.records if row.get("payload", {}).get("type") != "user_message"]
    source.write(rows)
    assert source.read() == []


def test_omp_agent_attribution_is_not_human_input(tmp_path):
    source = Source(tmp_path, "omp")
    for row in source.records:
        if row.get("message", {}).get("role") == "user":
            row["message"]["attribution"] = "agent"
    source.write(source.records)
    assert source.read() == []


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_project_change_excludes_whole_turn(tmp_path, client):
    source = Source(tmp_path, client)
    if client == "claude":
        source.records[-1]["cwd"] = str(tmp_path)
    else:
        source.records.insert(-1, {"type": "turn_context", "timestamp": "2026-01-01T00:00:03.5Z", "payload": {"cwd": str(tmp_path), "turn_id": "turn1"}})
    source.write(source.records)
    assert source.read() == []


@pytest.mark.parametrize("client", FILES)
def test_association_uses_deepest_repository_not_parent_container(tmp_path, client):
    source = Source(tmp_path, client)
    source.repositories.append({"id": "github.com:parent", "paths": [str(tmp_path)]})
    assert source.read()[0]["repository_id"] == "github.com:1"
    (tmp_path / "second").mkdir()
    other = Source(tmp_path / "second", client)
    other.repositories = [{"id": "github.com:child", "paths": [str(other.repo / "child")]}]
    assert other.read() == []


@pytest.mark.parametrize("client", FILES)
def test_blocked_nested_repository_never_falls_back_to_eligible_parent(tmp_path, client):
    source = Source(tmp_path, client)
    source.repositories = [
        {"id": "github.com:parent", "paths": [str(tmp_path)]},
        {"id": "blocked:nested", "paths": [str(source.repo)], "eligible": False},
    ]
    associations = []

    def eligible(repository, start, end):
        associations.append(repository)
        return repository == "github.com:parent"

    assert source.read(eligible) == []
    assert associations == ["blocked:nested"]


@pytest.mark.parametrize("client", FILES)
def test_cancellation_interrupts_stream_without_waiting_for_a_turn(tmp_path, client, monkeypatch):
    from actomasto.adapters import PARSERS

    source = Source(tmp_path, client)
    parser = PARSERS[client]
    parse = parser.parse
    stopped = False

    def stop_after_record(self, row, context, offset):
        nonlocal stopped
        event = parse(self, row, context, offset)
        stopped = True
        return event

    def cancelled():
        if stopped:
            raise SourceCancelled()

    with monkeypatch.context() as patch:
        patch.setattr(parser, "parse", stop_after_record)
        with pytest.raises(SourceCancelled):
            source.read(cancelled=cancelled)
    assert source.cursors == {}
    assert len(source.read()) == 1


@pytest.mark.parametrize("eligible_result", [True, False])
def test_cancellation_before_referenced_text_or_rejection_leaves_turn_retryable(tmp_path, eligible_result):
    source = Source(tmp_path, "omp")
    stopped = False

    def eligible(repository, start, end):
        nonlocal stopped
        stopped = True
        return eligible_result

    def cancelled():
        if stopped:
            raise SourceCancelled()

    with pytest.raises(SourceCancelled):
        source.read(eligible, cancelled)
    assert source.cursors == {}
    assert len(source.read()) == 1


def test_cancellation_after_yield_does_not_mark_turn_collected(tmp_path):
    source = Source(tmp_path, "omp")
    stopped = False

    def cancelled():
        if stopped:
            raise SourceCancelled()

    stream = collect(source.client, source.root, source.repositories,
                     lambda key: copy.deepcopy(source.cursors.get(key)),
                     lambda key, value: source.cursors.__setitem__(key, copy.deepcopy(value)),
                     lambda repository, start, end: True, cancelled)
    unit = next(stream)
    stopped = True
    with pytest.raises(SourceCancelled):
        next(stream)
    assert source.cursors == {}
    assert source.read() == [unit]


def test_truncation_rescans_without_losing_unfinished_turn(tmp_path):
    source = Source(tmp_path, "omp")
    source.write(source.records[:-1])
    assert source.read() == []
    source.write(source.records[:3])
    assert source.read() == []
    source.write(source.records)
    assert len(source.read()) == 1


def test_missing_message_time_excludes_whole_turn(tmp_path):
    source = Source(tmp_path, "claude")
    source.records[2]["timestamp"] = "not-a-timestamp"
    source.write(source.records)
    assert source.read() == []


def test_claude_conflicting_origin_never_becomes_human_via_prompt_source(tmp_path):
    source = Source(tmp_path, "claude")
    source.records[0].update(origin={"kind": "task-notification"}, promptSource="typed")
    source.write(source.records)
    assert source.read() == []


def test_codex_missing_turn_context_does_not_guess_project_provenance(tmp_path):
    source = Source(tmp_path, "codex")
    source.write([row for row in source.records if row["type"] != "turn_context"])
    assert source.read() == []


@pytest.mark.parametrize("client", FILES)
def test_unrecognized_version_is_not_guessed_compatible(tmp_path, client):
    source = Source(tmp_path, client)
    if client == "claude":
        source.records[0]["version"] = "999.0.0"
    elif client == "codex":
        source.records[0]["payload"]["cli_version"] = "999.0.0"
    else:
        source.records[1]["version"] = 999
    source.write(source.records)
    with pytest.raises(AdapterError, match="unsupported_version"):
        source.read()


def test_codex_unobserved_fork_schema_pauses_instead_of_guessing_lineage(tmp_path):
    source = Source(tmp_path, "codex")
    source.records[0]["payload"]["forked_from_id"] = "unverified-parent"
    source.write(source.records)
    with pytest.raises(AdapterError, match="unknown_session_schema"):
        source.read()
