"""Deterministic lifecycle regressions; all activity is authored synthetic data."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from actomasto.store import DAY, MODEL, Store

NOW = datetime(2026, 1, 20, 12, tzinfo=timezone.utc).timestamp()
REPO = "github.com:17"
ORIGIN = "github.com/example/project"


def configuration(root, cap="20.00"):
    scope = root / "scope.json"
    if not scope.exists():
        scope.write_text(json.dumps({"version": 1, "roots": {"claude": [], "codex": [], "omp": []}}))
    return {"version": 2, "discovery": {"roots": [str(root)]},
            "identity": {"author_emails": ["author@example.invalid"]},
            "blocklist": {"repositories": [], "paths": [], "text": [], "scoped": []},
            "funes": {"executable": str(root / "funes"), "corpus": str(root / "corpus"), "scope": str(scope)},
            "generation": {"model": MODEL, "character_limit": 500, "interval_minutes": 30},
            "budget": {"monthly_usd": cap, "timezone": "UTC"}, "notifications": {"enabled": False}}


def discovered(root, origin=ORIGIN, name="clone"):
    host, project = origin.split("/", 1)
    return {"path": str(root / name), "origin": origin, "host": host,
            "project_path": project, "reason": None}


def activity(name="one", at=NOW - 100, repository=REPO, equivalent=None):
    return {"id": name, "repository_id": repository, "repository_display": ORIGIN,
            "kind": "commit", "event_time": at, "event_end": at,
            "equivalent_id": equivalent, "source_ref": {"commit": name}, "paths": ["module.py"],
            "items": [{"id": name + "-evidence", "text": "Added a deterministic parser for the example format.",
                       "provenance": "committed", "event_time": at, "source_ref": {"commit": name}}],
            "adapter": "git", "adapter_version": "git-1", "partial_source": False}


def enroll(store, root, now=NOW, entries=None):
    entries = entries or [discovered(root)]
    return store.sync_repositories(entries, {ORIGIN: {"state": "public", "id": REPO}}, now)


@pytest.fixture
def opened(tmp_path):
    store = Store(tmp_path / "data")
    config = configuration(tmp_path)
    store.apply_config(config, NOW)
    store.set_enabled(True, NOW)
    enroll(store, tmp_path)
    yield store, tmp_path, config
    store.close()


def accepted(store, unit, now=NOW):
    assert store.enqueue(unit, now)
    attempt = store.reserve([unit["id"]], 100, MODEL, now)
    assert attempt is not None
    store.settle(attempt["id"], {"input_tokens": 100, "output_tokens": 20},
                 [{"text": "I added a deterministic parser.", "evidence_ids": [unit["items"][0]["id"]]}], now)
    return store.list_suggestions()[0]


def test_disabled_install_does_not_enroll(tmp_path):
    store = Store(tmp_path / "data")
    try:
        store.apply_config(configuration(tmp_path), NOW)
        assert enroll(store, tmp_path) == []
        assert not store.settings()["enabled"]
        assert not store.enqueue(activity(), NOW)
    finally:
        store.close()


def test_once_only_import_off_boundaries_and_restart(opened):
    store, root, config = opened
    original = store.repositories()[0]["import_start"]
    store.set_enabled(False, NOW + 10)
    store.set_enabled(True, NOW + 30)
    enroll(store, root, NOW + 30, [discovered(root), discovered(root, name="second")])
    assert store.repositories()[0]["import_start"] == original
    assert len(store.repositories()[0]["paths"]) == 2
    assert store.eligible(REPO, NOW + 9, NOW + 10)
    assert not store.eligible(REPO, NOW + 10, NOW + 10)
    assert not store.eligible(REPO, NOW + 9, NOW + 11)
    assert not store.eligible(REPO, NOW + 29, NOW + 29)
    assert store.eligible(REPO, NOW + 30, NOW + 30)
    assert store.eligible(REPO, NOW - 6 * DAY, NOW - 6 * DAY)
    store.purge(None, NOW + 30)
    assert store.repositories()[0]["import_start"] == original
    other = Store(root / "data")
    try:
        other.recover(NOW + 31)
        assert not other.eligible(REPO, NOW + 20, NOW + 20)
        assert other.eligible(REPO, NOW + 31, NOW + 31)
    finally:
        other.close()


def test_late_discovered_import_honors_already_recorded_off_interval(opened):
    store, root, config = opened
    store.set_enabled(False, NOW + 10)
    store.set_enabled(True, NOW + 100)
    second = discovered(root, "gitlab.com/example/other", "other")
    store.sync_repositories([discovered(root), second], {
        ORIGIN: {"state": "public", "id": REPO},
        second["origin"]: {"state": "public", "id": "gitlab.com:18"}}, NOW + 100)
    assert not store.eligible("gitlab.com:18", NOW + 50, NOW + 50)
    assert store.eligible("gitlab.com:18", NOW - DAY, NOW - DAY)


def test_expired_equivalent_rebase_remains_terminal_after_recovery(opened):
    store, root, config = opened
    first = activity(equivalent="same-change")
    assert store.enqueue(first, NOW)
    store.set_enabled(False, NOW + 1)
    store.recover(NOW + DAY)
    assert store.pending(NOW + DAY) == []
    store.set_enabled(True, NOW + DAY + 1)
    enroll(store, root, NOW + DAY + 1)
    rebased = activity("different-commit", NOW + DAY, equivalent="same-change")
    assert store.seen(rebased)
    assert not store.enqueue(rebased, NOW + DAY + 1)
    assert store.status(NOW + DAY + 1)["counters"]["expired"] == 1


def test_zero_candidates_and_purge_never_replay_but_new_work_can(opened):
    store, root, config = opened
    first = activity(equivalent="patch-one")
    assert store.enqueue(first, NOW)
    attempt = store.reserve([first["id"]], 10, MODEL, NOW)
    store.settle(attempt["id"], {"input_tokens": 10, "output_tokens": 1}, [], NOW)
    store.settle(attempt["id"], None, [{"text": "Duplicate", "evidence_ids": ["one-evidence"]}], NOW)
    assert store.list_suggestions() == []
    assert store.seen(activity("rebased", equivalent="patch-one"))
    store.purge(REPO, NOW)
    assert not store.enqueue(first, NOW)
    assert store.enqueue(activity("new-work", equivalent="patch-two"), NOW)
    assert store.status(NOW)["budget"]["spent_micro_usd"] == 15


def test_private_revokes_cached_public_context_and_does_not_reimport(opened):
    store, root, config = opened
    assert store.enqueue(activity(), NOW)
    start = store.repositories()[0]["import_start"]
    store.sync_repositories([discovered(root)], {ORIGIN: {"state": "private"}}, NOW + 1)
    assert not store.pending(NOW + 1)
    assert store.repositories()[0]["state"] == "private"
    enroll(store, root, NOW + 10)
    assert store.repositories()[0]["import_start"] == start
    assert not store.eligible(REPO, NOW + 5, NOW + 5)
    assert store.seen(activity())


def test_uncertainty_holds_until_original_expiry_and_adapter_isolation(opened):
    store, root, config = opened
    assert store.enqueue(activity(), NOW + 100)
    store.sync_repositories([discovered(root)], {ORIGIN: {"state": "uncertain"}}, NOW + DAY)
    assert len(store.pending(NOW + DAY)) == 1
    epoch = store.settings()["epoch"]
    assert not store.dispatch_allowed(["one"], epoch, NOW + DAY)
    enroll(store, root, NOW + DAY + 1)
    store.invalidate_adapter("claude", False, NOW + DAY + 1)
    assert store.dispatch_allowed(["one"], store.settings()["epoch"], NOW + DAY + 1)
    store.invalidate_adapter("git", False, NOW + DAY + 1)
    assert not store.dispatch_allowed(["one"], store.settings()["epoch"], NOW + DAY + 1)
    store.invalidate_adapter("git", True, NOW + DAY + 1)
    assert store.dispatch_allowed(["one"], store.settings()["epoch"], NOW + DAY + 1)
    assert not store.pending(NOW + DAY + 100)


@pytest.mark.parametrize("failed_adapter", ["claude", "git"])
def test_adapter_failure_only_fences_its_own_inflight_results(opened, failed_adapter):
    store, _, _ = opened
    assert store.enqueue(activity(), NOW)
    attempt = store.reserve(["one"], 10, MODEL, NOW)
    store.invalidate_adapter(failed_adapter, False, NOW + 1)
    store.settle(attempt["id"], {"input_tokens": 10, "output_tokens": 1},
                 [{"text": "I updated the project.", "evidence_ids": ["one-evidence"]}], NOW + 1)
    drafts = store.list_suggestions()
    if failed_adapter == "git":
        assert drafts == []
    else:
        assert [draft["text"] for draft in drafts] == ["I updated the project."]


def test_origin_change_in_one_clone_fences_queued_content(opened):
    store, root, config = opened
    enroll(store, root, NOW, [discovered(root), discovered(root, name="second")])
    assert store.enqueue(activity(), NOW)
    epoch = store.settings()["epoch"]
    store.sync_repositories([discovered(root, "github.com/example/private"), discovered(root, name="second")],
                           {ORIGIN: {"state": "public", "id": REPO}}, NOW + 1)
    assert not store.pending(NOW + 1)
    assert store.settings()["epoch"] != epoch
    assert store.repositories()[0]["paths"] == [str(root / "second")]


def test_budget_reservation_unknown_usage_recovery_and_cap_raise(opened):
    store, root, config = opened
    config["budget"]["monthly_usd"] = "0.02"
    store.apply_config(config, NOW)
    assert store.enqueue(activity(), NOW)
    attempt = store.reserve(["one"], 1000, MODEL, NOW)
    assert attempt["reservation_micro_usd"] == 11000
    store.recover(NOW + 1)
    assert store.status(NOW + 1)["budget"]["spent_micro_usd"] == 11000
    assert store.status(NOW + 1)["budget"]["reserved_micro_usd"] == 0
    assert store.reserve(["one"], 1000, MODEL, NOW + 61) is None
    assert store.settings()["budget_paused"]
    config["budget"]["monthly_usd"] = "0.04"
    store.apply_config(config, NOW + 100)
    assert not store.settings()["budget_paused"]
    assert not store.eligible(REPO, NOW + 80, NOW + 80)
    assert store.reserve(["one"], 1000, MODEL, NOW + 100) is not None


def test_attempt_crossing_month_charges_dispatch_period(opened):
    store, root, config = opened
    last = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    enroll(store, root, last)
    unit = activity(at=last)
    assert store.enqueue(unit, last)
    attempt = store.reserve([unit["id"]], 100, MODEL, last)
    store.settle(attempt["id"], None, [], last + 2)
    status = store.status(last + 2)
    assert status["budget"]["period"] != attempt["period"]
    assert status["budget"]["spent_micro_usd"] == 0
    assert store.db.execute("SELECT spent,reserved FROM periods WHERE id=?", (attempt["period"],)).fetchone()[:] == (10100, 0)


def test_retry_ceiling_and_malformed_ceiling_are_terminal(opened):
    store, root, config = opened
    assert store.enqueue(activity(), NOW)
    now = NOW
    for index, delay in enumerate((60, 300, 1800, 0)):
        attempt = store.reserve(["one"], 10, MODEL, now)
        assert attempt is not None
        store.settle(attempt["id"], None, None, now, error="network_error")
        if index < 3:
            assert store.reserve(["one"], 10, MODEL, now + delay - 1) is None
        now += delay
    assert not store.pending(now)
    assert store.seen(activity())
    assert store.status(now)["budget"]["spent_micro_usd"] == 40040
    assert store.enqueue(activity("malformed"), now)
    for index in range(2):
        attempt = store.reserve(["malformed"], 10, MODEL, now)
        store.settle(attempt["id"], None, None, now, error="malformed_output")
        now += 60
    assert not store.pending(now)


def test_off_and_purge_fence_late_candidates_without_losing_cost(opened):
    store, root, config = opened
    unit = activity()
    assert store.enqueue(unit, NOW)
    attempt = store.reserve(["one"], 100, MODEL, NOW)
    store.set_enabled(False, NOW + 1)
    candidate = [{"text": "I added a parser.", "evidence_ids": ["one-evidence"]}]
    store.settle(attempt["id"], {"input_tokens": 100, "output_tokens": 10}, candidate, NOW + 2)
    assert not store.list_suggestions()
    assert store.pending(NOW + 2)[0]["expires_at"] == NOW + DAY
    store.set_enabled(True, NOW + 100)
    attempt = store.reserve(["one"], 100, MODEL, NOW + 100)
    store.purge(REPO, NOW + 101)
    store.settle(attempt["id"], None, candidate, NOW + 102)
    assert not store.list_suggestions()
    assert not store.pending(NOW + 102)
    assert store.status(NOW + 102)["budget"]["spent_micro_usd"] == 10250


def test_split_keeps_expiry_retry_count_and_marks_skipped_items(opened):
    store, root, config = opened
    unit = activity(equivalent="parent-patch")
    unit["items"].append({**unit["items"][0], "id": "oversized-evidence"})
    assert store.enqueue(unit, NOW)
    attempt = store.reserve([unit["id"]], 100, MODEL, NOW)
    store.settle(attempt["id"], None, None, NOW, error="network_error")
    pieces = store.split(unit["id"], [{**unit, "items": [unit["items"][0]]}], NOW + 60,
                         skipped_item_ids=["oversized-evidence"])
    assert pieces[0]["expires_at"] == NOW + DAY
    assert pieces[0]["attempt_count"] == 1
    assert pieces[0]["partial_source"]
    assert store.seen(activity("rebased", equivalent="parent-patch"))
    assert store.status(NOW + 60)["counters"]["oversized_item"] == 1
    assert store.reserve([pieces[0]["id"]], 100, MODEL, NOW + 60) is not None


def test_export_recovers_append_ack_partial_corruption_and_missing_file(opened):
    store, root, config = opened
    draft = accepted(store, activity())
    store.export()
    expected = store.export_path.read_bytes()
    # A completed append whose database acknowledgment was lost is not appended twice.
    store.db.execute("UPDATE export_outbox SET exported=0")
    store.recover(NOW)
    assert store.export_path.read_bytes() == expected
    with store.export_path.open("ab") as stream:
        stream.write(b'{"partial":')
    store.recover(NOW)
    assert store.export_path.read_bytes() == expected
    assert any(event["code"] == "export_corrupt" for event in store.status(NOW)["events"])
    store.export_path.unlink()
    store.recover(NOW)
    assert store.export_path.read_bytes() == expected
    assert json.loads(expected)["id"] == draft["id"]


def test_export_failure_is_reported_and_purge_rebuild_retries(opened, monkeypatch):
    store, root, config = opened
    accepted(store, activity())
    store.export()
    replacement = os.replace
    def unavailable(*args):
        raise OSError("synthetic unavailable filesystem")
    monkeypatch.setattr(os, "replace", unavailable)
    with pytest.raises(OSError):
        store.purge(REPO, NOW)
    assert not store.list_suggestions()
    assert store.settings()["export_rebuild"]
    monkeypatch.setattr(os, "replace", replacement)
    store.recover(NOW)
    assert store.export_path.read_bytes() == b""
    assert store.seen(activity())


def test_export_only_public_metadata_and_evidence_opt_in(opened):
    store, root, config = opened
    unit = activity()
    unit.update(kind="conversation", adapter="claude", source_ref={"client": "claude", "session_id": "synthetic-session",
                "message_ids": ["message-one"], "path": "/private/synthetic/session.jsonl"})
    unit["items"][0].update(provenance="assistant_reported", source_ref={"message_ids": ["message-one"],
                "transcript_path": "/private/synthetic/session.jsonl"})
    store.invalidate_adapter("claude", True, NOW)
    record = accepted(store, unit)
    assert "evidence" not in store.show(record["id"])
    evidence = store.show(record["id"], evidence=True)["evidence"][0]
    assert evidence["provenance"] == "assistant_reported"
    assert evidence["source_ref"] == {"client": "claude", "session_id": "synthetic-session", "message_ids": ["message-one"]}
    store.export()
    assert b"/private/" not in store.export_path.read_bytes()
    for path in (store.path, store.export_path, store.data_dir / "state.sqlite3-wal", store.data_dir / "state.sqlite3-shm"):
        if path.exists():
            assert path.stat().st_mode & 0o777 == 0o600
    assert store.data_dir.stat().st_mode & 0o777 == 0o700


def test_backward_clock_never_reopens_off_boundary(opened):
    store, root, config = opened
    store.set_enabled(False, NOW + 100)
    store.set_enabled(True, NOW + 20)
    assert store.settings()["clock_uncertain"]
    assert not store.dispatch_allowed(["one"], store.settings()["epoch"], NOW + 30)
    store.expire(NOW + 101)
    assert not store.settings()["clock_uncertain"]
    assert store.eligible(REPO, NOW + 101, NOW + 101)
    assert store.eligible(REPO, NOW + 99, NOW + 99)


def test_scoped_purge_preserves_other_repository_history_and_spend(opened):
    store, root, config = opened
    other_origin = "gitlab.com/example/other"
    other_id = "gitlab.com:18"
    store.sync_repositories([discovered(root), discovered(root, other_origin, "other")], {
        ORIGIN: {"state": "public", "id": REPO},
        other_origin: {"state": "public", "id": other_id}}, NOW)
    first = accepted(store, activity())
    other = accepted(store, activity("other", repository=other_id))
    before = store.status(NOW)["budget"]["spent_micro_usd"]
    store.purge(REPO, NOW)
    assert store.show(first["id"], evidence=True) is None
    assert store.show(other["id"], evidence=True)["evidence"]
    assert [row["id"] for row in store.list_suggestions(repo=other_id)] == [other["id"]]
    assert store.status(NOW)["budget"]["spent_micro_usd"] == before
    assert [json.loads(line)["id"] for line in store.export_path.read_text().splitlines()] == [other["id"]]
    assert store.seen(activity())
    assert store.seen(activity("other", repository=other_id))


def test_queue_rejection_is_terminal_without_evicting_retained_unit(opened):
    store, root, config = opened
    assert store.enqueue(activity(), NOW)
    large = activity("large")
    large["items"][0]["text"] = "x" * (10 * 1024 * 1024)
    assert not store.enqueue(large, NOW)
    assert [unit["id"] for unit in store.pending(NOW)] == ["one"]
    assert store.seen(large)
    assert store.status(NOW)["counters"]["queue_full"] == 1
    assert not store.enqueue(large, NOW)
    assert store.status(NOW)["counters"]["queue_full"] == 1
    assert not store.enqueue({**large, "id": "second-large"}, NOW)
    assert store.status(NOW)["counters"]["queue_full"] == 2


def test_cancelled_before_send_does_not_consume_retry_or_spend(opened):
    store, root, config = opened
    assert store.enqueue(activity(), NOW)
    for _ in range(5):
        attempt = store.reserve(["one"], 100, MODEL, NOW)
        assert attempt
        store.settle(attempt["id"], {"input_tokens": 0, "output_tokens": 0}, None, NOW, error="cancelled")
    assert store.pending(NOW)[0]["attempt_count"] == 0
    assert store.status(NOW)["budget"]["spent_micro_usd"] == 0
    assert store.reserve(["one"], 100, MODEL, NOW)


def test_timezone_change_waits_for_open_period_and_does_not_enable_at_reset(opened):
    store, root, config = opened
    original = store.status(NOW)["budget"]["period"]
    config["budget"].update(timezone="America/Los_Angeles", monthly_usd="0.00")
    store.apply_config(config, NOW)
    assert store.enqueue(activity(), NOW)
    assert store.reserve(["one"], 100, MODEL, NOW) is None
    store.set_enabled(False, NOW + 1)
    assert store.status(NOW + 1)["budget"]["period"] == original
    reset = datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
    status = store.status(reset)
    assert not status["enabled"]
    assert status["budget"]["timezone"] == "America/Los_Angeles"
    assert status["budget"]["period"] != original


def test_cached_verification_cannot_extend_public_ttl(opened):
    store, root, config = opened
    cached = {ORIGIN: {"state": "public", "id": REPO, "checked_at": NOW, "expires_at": NOW + DAY}}
    store.sync_repositories([discovered(root)], cached, NOW + 100)
    assert store.repositories()[0]["public_until"] == NOW + DAY
    store.sync_repositories([discovered(root)], cached, NOW + DAY)
    assert store.repositories()[0]["state"] == "uncertain"
    assert store.repositories()[0]["public_until"] == NOW + DAY
    store.sync_repositories([discovered(root)], {ORIGIN: {"state": "private"}}, NOW + DAY + 1)
    store.sync_repositories([discovered(root)], cached, NOW + DAY + 2)
    assert store.repositories()[0]["state"] == "private"
    enroll(store, root, NOW + DAY + 3)
    assert store.repositories()[0]["state"] == "public"
    assert not store.eligible(REPO, NOW + DAY + 2, NOW + DAY + 2)


def test_retry_deadline_includes_count_deferral_not_fresh_or_reserved(opened):
    store, root, config = opened
    assert store.enqueue(activity("fresh"), NOW)
    assert store.next_retry(NOW) is None
    assert store.enqueue(activity("count"), NOW)
    store.defer(["count"], NOW, 120, "rate_limited")
    assert store.next_retry(NOW) == NOW + 120
    assert store.enqueue(activity("retry"), NOW)
    attempt = store.reserve(["retry"], 10, MODEL, NOW)
    store.settle(attempt["id"], None, None, NOW, error="network_error")
    assert store.next_retry(NOW) == NOW + 60
    # Due deadlines remain visible rather than being mistaken for fresh work.
    assert store.next_retry(NOW + 61) == NOW + 60
    attempt = store.reserve(["retry"], 10, MODEL, NOW + 61)
    assert attempt is not None
    assert store.next_retry(NOW + 61) == NOW + 120
    store.defer(["count"], NOW + 61, DAY, "rate_limited")
    assert store.next_retry(NOW + 61) is None
    assert store.next_retry(NOW + DAY) is None


def test_author_removal_discards_reserved_git_without_replaying_history(opened):
    store, root, config = opened
    draft = accepted(store, activity("accepted"))
    unit = activity(equivalent="original-patch")
    assert store.enqueue(unit, NOW)
    attempt = store.reserve([unit["id"]], 10, MODEL, NOW)
    original_repo = store.repositories()[0]
    config["identity"]["author_emails"] = ["replacement@example.invalid"]
    store.apply_config(config, NOW + 1)
    assert not store.pending(NOW + 1)
    store.settle(attempt["id"], {"input_tokens": 10, "output_tokens": 10},
                 [{"text": "Old author work.", "evidence_ids": ["one-evidence"]}], NOW + 2)
    assert [entry["id"] for entry in store.list_suggestions()] == [draft["id"]]
    assert store.seen(activity("rebased", equivalent="original-patch"))
    assert store.repositories()[0]["import_start"] == original_repo["import_start"]
    assert store.eligible(REPO, NOW - 50, NOW - 50)
    config["identity"]["author_emails"].append("author@example.invalid")
    store.apply_config(config, NOW + 3)
    assert not store.enqueue(unit, NOW + 3)


def test_source_root_change_discards_only_affected_pending_units(opened):
    store, root, config = opened
    scope = Path(config["funes"]["scope"])
    enrollment = {"version": 1, "roots": {"claude": [str(root / "old-claude")],
                                        "codex": [str(root / "codex")], "omp": []}}
    scope.write_text(json.dumps(enrollment))
    store.apply_config(config, NOW)
    git = activity("git")
    claude = {**activity("claude"), "adapter": "claude", "kind": "conversation"}
    codex = {**activity("codex"), "adapter": "codex", "kind": "conversation"}
    for unit in (git, claude, codex):
        assert store.enqueue(unit, NOW)
    original = store.repositories()[0]
    enrollment["roots"]["claude"] = [str(root / "new-claude")]
    scope.write_text(json.dumps(enrollment))
    store.apply_config(config, NOW + 1)
    assert {unit["id"] for unit in store.pending(NOW + 1)} == {"git", "codex"}
    assert store.seen(claude)
    assert store.repositories()[0] == original
    enrollment["roots"]["claude"] = [str(root / "old-claude")]
    scope.write_text(json.dumps(enrollment))
    store.apply_config(config, NOW + 2)
    assert not store.enqueue(claude, NOW + 2)


def test_source_health_summarizes_streams_without_private_metadata(opened):
    store, root, config = opened
    store.save_cursor("adapter-stream:claude:private-path", {
        "pending_turns": 2, "context": {"cwd": "/private/project"},
        "quarantine": "malformed_record"})
    store.save_cursor("adapter-stream:claude:another-private-path", {
        "pending_turns": 1, "error": "unsupported_version"})
    store.save_cursor("adapter-stream:codex:private-path", {
        "pending_turns": 0, "error": "/private/failure.log"})
    store.save_cursor("adapter-unit:claude:unit", {"reason": "collected"})
    store.save_cursor("discovery_status", {"candidates": ["/private/project"]})
    assert store.source_health() == {
        "claude": {"streams": 2, "pending_turns": 3, "quarantined_streams": 1,
                   "errors": ["malformed_record", "unsupported_version"]},
        "codex": {"streams": 1, "pending_turns": 0, "quarantined_streams": 0,
                  "errors": ["source_error"]},
    }


def test_exact_funes_migration_preserves_lifecycle_and_repairs_config_after_crash(tmp_path, monkeypatch):
    import hashlib
    import sqlite3
    from actomasto import cli
    from actomasto.config import load, validate
    from actomasto.funes_source import FunesSource, VERSIONS

    config = configuration(tmp_path)
    enrollment = {"version": 1, "roots": {client: [str(tmp_path / client)]
                                         for client in ("claude", "codex", "omp")}}
    Path(config["funes"]["scope"]).write_text(json.dumps(enrollment))
    legacy = dict(config, version=1, sources={client + "_root": roots[0]
                                            for client, roots in enrollment["roots"].items()})
    del legacy["funes"]
    config_path = tmp_path / "config"
    config_path.mkdir()
    # Author a real v1 TOML file; the v2 writer intentionally cannot write v1.
    text = ["version = 1"]
    for section, values in legacy.items():
        if section == "version":
            continue
        text.append(f"[{section}]")
        text.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
    (config_path / "config.toml").write_text("\n".join(text))
    store = Store(tmp_path / "data")
    store.apply_config(config, NOW)
    store.set_enabled(True, NOW)
    enroll(store, tmp_path)

    def conversation(label):
        fallback = hashlib.sha256(f"session::1:{label}".encode()).hexdigest()
        identity = hashlib.sha256(f"claude:session:{fallback}".encode()).hexdigest()
        unit = activity(identity)
        unit.update(kind="conversation", adapter="claude", adapter_version="claude-schema2",
                    source_ref={"client": "claude", "session_id": "session", "message_ids": [fallback]})
        return unit

    expired = conversation("expired")
    store.enqueue(expired, NOW)
    store.expire(NOW + DAY)
    at = NOW + DAY + 10
    enroll(store, tmp_path, at)
    purged = conversation("purged")
    store.enqueue(purged, at)
    store.purge(REPO, at)
    store.invalidate_adapter("claude", True, at)
    accepted_unit = conversation("accepted")
    accepted(store, accepted_unit, at)
    excluded = conversation("excluded")
    store.mark(excluded, "blocked")
    pending = conversation("pending")
    assert store.enqueue(pending, at)
    git = activity("pending-git")
    assert store.enqueue(git, at)
    unseen = conversation("unseen")
    for unit, reason in ((expired, "expired"), (purged, "purged"),
                         (accepted_unit, "collected"), (excluded, "blocked"), (pending, "collected")):
        store.save_cursor("adapter-unit:claude:" + unit["id"], {"reason": reason})
    store.save_cursor("adapter-stream:claude:legacy", {"offset": 713, "inode": 321,
                                                      "pending_turns": 1, "quarantine": None})
    store.set_enabled(False, at + 1)
    store.set_enabled(True, at + 3)
    # Freeze the exact pre-upgrade schema/config as an installed v1 database.
    settings = store.settings()
    settings["config"] = validate(legacy, legacy=True)
    settings.pop("funes_scope", None)
    store._save_settings(settings)
    store.db.execute("PRAGMA user_version=1")
    tables = ("intervals", "repositories", "source_cursors", "source_markers", "pending_units",
              "periods", "attempts", "suggestions", "evidence", "export_outbox", "events",
              "event_states", "counters")
    before = {table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
              for table in tables}
    store.close()
    with pytest.raises(ValueError, match="migration_required"):
        Store(tmp_path / "data")
    monkeypatch.setattr(FunesSource, "capabilities", lambda self: {"protocol": 1, "harnesses": dict(VERSIONS)})
    real_save = cli.save
    def interrupted_save(*args):
        raise OSError("synthetic failure after database commit")
    monkeypatch.setattr(cli, "save", interrupted_save)
    paths = {"config": config_path, "data": tmp_path / "data"}
    with pytest.raises(OSError):
        cli.migrate_configuration(paths, config["funes"])
    reopened = Store(tmp_path / "data")
    try:
        after = {table: [tuple(row) for row in reopened.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                 for table in tables}
        assert after == before
        migrated_settings = reopened.settings()
        assert {k: v for k, v in migrated_settings.items()
                if k not in ("config", "funes_scope", "funes_migration")} == {
                    k: v for k, v in settings.items() if k != "config"}
        assert migrated_settings["funes_migration"] == "complete"
        assert all(reopened.seen(unit) for unit in (expired, purged, accepted_unit, excluded, pending))
        assert not reopened.seen(unseen)
        assert not reopened.eligible(REPO, at + 2, at + 2)
        assert reopened.dispatch_allowed([git["id"]], settings["epoch"], at + 4)
        assert not reopened.dispatch_allowed([pending["id"]], settings["epoch"], at + 4)
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        reopened.close()
    monkeypatch.setattr(cli, "save", real_save)
    cli.migrate_configuration(paths, config["funes"])
    assert load(config_path / "config.toml") == validate(config)
    # The released binary's version gate rejects this database before reading settings.
    with sqlite3.connect(tmp_path / "data" / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] > 1


def test_failed_migration_keeps_git_usable_without_reopening_conversation_health(tmp_path, monkeypatch):
    from actomasto.config import ConfigError, migration_config
    from actomasto.funes_source import FunesSource, SourceError

    config = configuration(tmp_path)
    roots = {client: [str(tmp_path / client)] for client in ("claude", "codex", "omp")}
    Path(config["funes"]["scope"]).write_text(json.dumps({"version": 1, "roots": roots}))
    legacy = dict(config, version=1, sources={client + "_root": values[0]
                                            for client, values in roots.items()})
    del legacy["funes"]
    store = Store(tmp_path / "data")
    store.apply_config(config, NOW)
    store.set_enabled(True, NOW)
    enroll(store, tmp_path)
    assert store.enqueue(activity(), NOW)
    settings = store.settings()
    settings["config"] = legacy
    store._save_settings(settings)
    store.db.execute("PRAGMA user_version=1")
    store.close()
    store = Store(tmp_path / "data", migrate=True)
    try:
        def unavailable(self):
            raise SourceError("source_unavailable")
        monkeypatch.setattr(FunesSource, "capabilities", unavailable)
        with pytest.raises(SourceError):
            store.migrate_config(migration_config(legacy, config["funes"]))
        monkeypatch.setattr(FunesSource, "capabilities", lambda self: {"harnesses": {"claude": "future-schema"}})
        with pytest.raises(SourceError, match="unsupported_harness"):
            store.migrate_config(migration_config(legacy, config["funes"]))
        assert store.settings()["funes_migration"] == "pending"
        assert store.settings()["enabled"]
        assert store.dispatch_allowed(["one"], settings["epoch"], NOW)
        assert not any(row[0] for row in store.db.execute(
            "SELECT healthy FROM adapters WHERE adapter IN ('claude','codex','omp')"))
        roots["omp"] = []
        Path(config["funes"]["scope"]).write_text(json.dumps({"version": 1, "roots": roots}))
        with pytest.raises(ConfigError, match="migration_scope_mismatch"):
            migration_config(legacy, config["funes"])
        assert store.settings()["config"] == legacy
    finally:
        store.close()


def test_completed_migration_retry_cannot_expand_enrollment(opened, monkeypatch):
    from actomasto.config import ConfigError
    from actomasto.funes_source import FunesSource, VERSIONS
    store, root, config = opened
    settings = store.settings()
    settings["funes_migration"] = "complete"
    store._save_settings(settings)
    pending = {**activity("conversation"), "adapter": "omp", "kind": "conversation"}
    assert store.enqueue(pending, NOW)
    before = store.settings()
    row = tuple(store.db.execute("SELECT * FROM pending_units").fetchone())
    scope = Path(config["funes"]["scope"])
    scope.write_text(json.dumps({"version": 1, "roots": {"claude": [], "codex": [], "omp": [str(root / "new")]}}))
    monkeypatch.setattr(FunesSource, "capabilities", lambda self: {"harnesses": dict(VERSIONS)})
    with pytest.raises(ConfigError, match="migration_scope_changed"):
        store.migrate_config(config)
    assert store.settings() == before
    assert tuple(store.db.execute("SELECT * FROM pending_units").fetchone()) == row


def test_scope_mutation_invalidates_final_dispatch_without_epoch_change(opened):
    store, root, config = opened
    pending = {**activity("conversation"), "adapter": "omp", "kind": "conversation"}
    assert store.enqueue(pending, NOW)
    store.invalidate_adapter("omp", True, NOW)
    epoch = store.settings()["epoch"]
    assert store.dispatch_allowed([pending["id"]], epoch, NOW)
    Path(config["funes"]["scope"]).write_text(json.dumps({
        "version": 1, "roots": {"claude": [], "codex": [], "omp": [str(root / "changed")]}}))
    assert not store.dispatch_allowed([pending["id"]], epoch, NOW)
    assert store.settings()["epoch"] == epoch
