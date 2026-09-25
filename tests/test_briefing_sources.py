"""Synthetic Git, inventory and complete Funes protocol evidence; no hosted calls."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess

import pytest

from actomasto import briefing_sources as sources
from actomasto.funes_source import FunesSource, VERSIONS

NOW = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp())
AUTHOR = "owner@example.test"


class Repository:
    def __init__(self, path, origin="https://github.com/Owner/Project.git"):
        self.path = path
        self.path.mkdir(parents=True)
        self.head = None
        self.run("init", "--initial-branch=main", "--template=")
        if origin:
            self.run("remote", "add", "origin", origin)

    def run(self, *args, data=None, email=AUTHOR, stamp=STAMP - 100):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": "Other Committer", "GIT_COMMITTER_EMAIL": "committer@example.test",
               "GIT_AUTHOR_DATE": f"{STAMP - 90 * 86400} +0000", "GIT_COMMITTER_DATE": f"{stamp} +0000"}
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.path), *args],
                              input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=True, env=env).stdout

    def commit(self, files, *, summary="Implement behavior", email=AUTHOR, stamp=STAMP - 100, ref="refs/heads/main"):
        self.run("read-tree", self.head or "--empty")
        for name, text in files.items():
            oid = self.run("hash-object", "-w", "--stdin", data=text.encode()).decode().strip()
            self.run("update-index", "--add", "--cacheinfo", "100644", oid, name)
        tree = self.run("write-tree").decode().strip()
        parents = ["-p", self.head] if self.head else []
        oid = self.run("commit-tree", tree, *parents, data=summary.encode(), email=email, stamp=stamp).decode().strip()
        self.run("update-ref", ref, oid)
        if ref == "refs/heads/main":
            self.head = oid
            self.run("reset", "--hard", oid)
        return oid


def config(root, **extra):
    return {"roots": [str(root)], "author_emails": [AUTHOR], "timezone": "America/Los_Angeles",
            "remote_hosts": [], "blocklist": {}, **extra}


def evidence(bundle, kind):
    return [item for project in bundle["projects"] for item in project["evidence"] if item["kind"] == kind]


def test_wrong_author_and_remote_only_history_do_not_count(tmp_path):
    repo = Repository(tmp_path / "repo")
    ours = repo.commit({"main.py": "print('owner')"}, summary="Owner source work")
    repo.commit({"foreign.py": "print('foreign')"}, email="someone@example.test", summary="Foreign source work")
    repo.commit({"upstream.py": "print('upstream')"}, summary="Remote-only source work", ref="refs/remotes/upstream/main")
    bundle = sources.collect(config(tmp_path), now=NOW)
    commits = evidence(bundle, "commit")
    assert [entry["text"].splitlines()[0] for entry in commits] == ["Owner source work"]
    assert commits[0]["source"].endswith("@" + ours)
    assert commits[0]["provenance"] == "committed"


def test_generated_dependencies_and_lockfiles_are_not_personal_source_activity(tmp_path):
    repo = Repository(tmp_path / "repo")
    repo.commit({"node_modules/pkg/index.js": "module.exports = 1", "uv.lock": "version = 1", "dist/app.js": "compiled"},
                summary="Generated dependencies")
    repo.commit({"src/app.py": "print('real')", "package-lock.json": "{}"}, summary="Actual source implementation")
    bundle = sources.collect(config(tmp_path), now=NOW)
    commits = evidence(bundle, "commit")
    assert [entry["text"].splitlines()[0] for entry in commits] == ["Actual source implementation"]
    assert "src/app.py" in commits[0]["text"]
    assert "package-lock.json" not in commits[0]["text"]


def test_committer_date_and_out_of_order_thirty_day_boundary(tmp_path):
    repo = Repository(tmp_path / "repo")
    start = STAMP - 30 * 86400
    boundary = repo.commit({"a.py": "a = 1"}, summary="At boundary", stamp=start)
    repo.commit({"b.py": "b = 1"}, summary="Older descendant", stamp=start - 1)
    latest = repo.commit({"c.py": "c = 1"}, summary="Latest valid", stamp=STAMP - 1)
    repo.commit({"d.py": "d = 1"}, summary="Future commit", stamp=STAMP + 1)
    bundle = sources.collect(config(tmp_path), now=NOW)
    commits = evidence(bundle, "commit")
    assert [entry["text"].splitlines()[0] for entry in commits] == ["Latest valid", "At boundary"]
    assert [entry["time"] for entry in commits] == [sources._iso(STAMP - 1), sources._iso(start)]
    assert commits[0]["source"].endswith(latest)
    assert commits[1]["source"].endswith(boundary)
    assert bundle["projects"][0]["last_activity"] == sources._iso(STAMP - 1)


def test_current_dirty_and_planning_are_undated_not_accepted_intent(tmp_path):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "print('old')"}, stamp=STAMP - 40 * 86400)
    (repo.path / "main.py").write_text("print('dirty')")
    (repo.path / "new").mkdir()
    (repo.path / "new" / "feature.py").write_text("print('untracked')")
    (repo.path / "README.md").write_text("Maybe rewrite everything next month.")
    (repo.path / "uv.lock").write_text("dependency churn")
    bundle = sources.collect(config(tmp_path), now=NOW)
    assert evidence(bundle, "commit") == []
    assert bundle["projects"][0]["last_activity"] is None
    dirty = [item for item in evidence(bundle, "working_tree") if "uncommitted" in item["text"]]
    assert len(dirty) == 1
    assert "main.py" in dirty[0]["text"] and "new/feature.py" in dirty[0]["text"]
    assert "uv.lock" not in dirty[0]["text"]
    assert dirty[0]["time"] is None
    assert bundle["projects"][0]["unfinished"] == [dirty[0]["text"]]
    assert all(item["time"] is None and item["provenance"] == "observed" for item in evidence(bundle, "planning"))


def test_duplicate_origins_merge_shared_commits_but_retain_clone_divergence(tmp_path, monkeypatch):
    first = Repository(tmp_path / "one")
    common = first.commit({"main.py": "print('base')"}, summary="Shared history")
    clone = tmp_path / "two"
    first.run("clone", "--no-hardlinks", str(first.path), str(clone))
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", "git@github.com:owner/project.git"], check=True)
    (clone / "main.py").write_text("print('clone unfinished')")
    # A shared immutable commit must not consume the second checkout's budget
    # again and suppress its distinct unfinished working-tree observation.
    monkeypatch.setattr(sources, "MAX_GIT_CALLS", 11)
    bundle = sources.collect(config(tmp_path), now=NOW)
    assert len(bundle["projects"]) == 1
    commits = evidence(bundle, "commit")
    assert len(commits) == 1
    assert str(first.path) + "@" + common in commits[0]["source"]
    assert str(clone) + "@" + common in commits[0]["source"]
    dirty = [item for item in evidence(bundle, "working_tree") if "uncommitted" in item["text"]]
    assert len(dirty) == 1 and str(clone) in dirty[0]["source"]


def test_nested_repository_and_worktree_are_discovered(tmp_path):
    parent = Repository(tmp_path / "parent")
    parent.commit({"main.py": "print('parent')"})
    nested = Repository(parent.path / "nested", origin="https://github.com/owner/nested.git")
    nested.commit({"child.py": "print('nested')"})
    worktree = tmp_path / "checkout"
    parent.run("worktree", "add", "-b", "alternate", str(worktree))
    (worktree / "main.py").write_text("print('unfinished worktree')")
    bundle = sources.collect(config(tmp_path), now=NOW)
    assert {item["id"] for item in bundle["projects"]} == {"git:github.com/owner/project", "git:github.com/owner/nested"}
    assert any(str(worktree) in item["source"] and "uncommitted" in item["text"] for item in evidence(bundle, "working_tree"))


def test_policy_repository_paths_text_and_secrets_fail_closed(tmp_path):
    allowed = Repository(tmp_path / "allowed")
    allowed.commit({"main.py": "print('safe')"}, summary="Useful source change")
    allowed.commit({"private/notes.txt": "hidden"}, summary="Blocked path title")
    allowed.commit({"blocked.py": "x = 1"}, summary="PRIVATE TOPIC title")
    allowed.commit({".env": "SECRET=data"}, summary="Secret-file title")
    allowed.commit({"safe.py": "x = 1"}, summary="API_KEY=ghp_abcdefghijklmnopqrstuvwxyz123456\nRedactable title")
    blocked = Repository(tmp_path / "blocked", origin="https://github.com/owner/blocked.git")
    blocked.commit({"main.py": "print('hidden')"}, summary="Blocked repository title")
    (allowed.path / "README.md").write_text("PRIVATE TOPIC hidden context")
    settings = config(tmp_path, blocklist={"repositories": ["github.com/owner/blocked"],
                                          "paths": ["private/**"], "text": ["PRIVATE TOPIC"]})
    bundle = sources.collect(settings, now=NOW)
    serialized = json.dumps(bundle["projects"])
    assert {project["id"] for project in bundle["projects"]} == {"git:github.com/owner/project"}
    for hidden in ("Blocked path title", "PRIVATE TOPIC", "Secret-file title", "Blocked repository title", "ghp_abcdefghijklmnopqrstuvwxyz123456"):
        assert hidden not in serialized
    assert "Useful source change" in serialized and "REDACTED_SECRET" in serialized


def test_policy_checks_full_planning_before_excerpt(tmp_path):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    (repo.path / "README.md").write_text("Visible planning\n" + "ordinary context\n" * 500 + "BLOCKED_AT_END")
    bundle = sources.collect(config(tmp_path, blocklist={"text": ["BLOCKED_AT_END"]}), now=NOW)
    assert evidence(bundle, "planning") == []


def test_missing_host_leaves_available_local_evidence_and_coverage(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"}, summary="Available local work")
    def unavailable(*args):
        raise sources.BriefingSourceError("source_timeout")
    monkeypatch.setattr(sources, "_remote", unavailable)
    bundle = sources.collect(config(tmp_path, remote_hosts=[{"name": "missing.local", "root": "~/code"}]), now=NOW)
    assert evidence(bundle, "commit")[0]["text"].startswith("Available local work")
    assert any("missing.local: unavailable" in note for note in bundle["coverage"])


def test_remote_worker_expands_home_without_shell_and_preserves_host(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "code" / "repo")
    repo.commit({"main.py": "x = 1"}, summary="Remote authored work")
    original = sources._run
    def local_transport(command, **kwargs):
        if command[0] == "ssh":
            assert "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
            return original(shlex.split(command[-1]), **{**kwargs, "env": {**os.environ, "HOME": str(tmp_path)}})
        return original(command, **kwargs)
    monkeypatch.setattr(sources, "_run", local_transport)
    settings = config(tmp_path, roots=[], remote_hosts=[{"name": "macbook.local", "root": "~/code"}])
    bundle = sources.collect(settings, now=NOW)
    assert bundle["projects"][0]["hosts"] == ["macbook.local"]
    assert evidence(bundle, "commit")[0]["source"].startswith("macbook.local:")
    assert evidence(bundle, "commit")[0]["text"].startswith("Remote authored work")


def test_inventory_discovers_non_git_without_trusting_timestamps_or_suggestions(tmp_path):
    project = tmp_path / "notes"
    project.mkdir()
    (project / "README.md").write_text("Exploratory non-Git research.")
    database = tmp_path / "inventory.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE projects(path TEXT, name TEXT, has_git INTEGER, scanned_at TEXT, last_mtime TEXT, last_commit_at TEXT, next_steps TEXT)")
        db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?)", (str(project), "notes", 0, "2025-01-01T00:00:00Z",
                                                                  NOW.isoformat(), NOW.isoformat(), "Launch immediately"))
    bundle = sources.collect(config(tmp_path, inventory_db=str(database)), now=NOW)
    assert len(bundle["projects"]) == 1
    assert bundle["projects"][0]["last_activity"] is None
    assert bundle["projects"][0]["unfinished"] == []
    assert "Launch immediately" not in json.dumps(bundle)
    assert evidence(bundle, "inventory")[0]["time"] is None
    assert "2025-01-01" in evidence(bundle, "inventory")[0]["text"]


def install_funes_protocol(monkeypatch, root, *, user_text="Defer caching. Next inspect eviction boundaries.",
                           assistant_text="I reported the checks passing; not independently verified.", coverage="current", status="complete",
                           age_days=0):
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()
    when = STAMP - 3600 - age_days * 86400
    items = [{"id": digest(f"omp:session-one:{message}"), "message_id": message, "role": role,
              "time": when + index, "identity": {"native_id": message, "record_index": index,
              "turn_context": "turn-one", "raw_record_digest": "a" * 64}, "text": text}
             for index, (message, role, text) in enumerate((("user-one", "user", user_text), ("assistant-one", "assistant", assistant_text)))]
    turn = {"ordinal": 0, "id": digest("omp:session-one:user-one"), "session_id": "session-one", "user_id": "user-one",
            "start": when, "end": when + 1, "invalid": False,
            "boundaries": [{"cwd": str(root), "time": when, "end": when + 1, "invalid": False}],
            "items": items, "message_ids": ["user-one", "assistant-one"],
            "bytes": sum(len(item["text"].encode()) for item in items)}
    metadata = {**turn, "items": [{key: value for key, value in item.items() if key != "text"} for item in items]}
    calls = []
    def request(self, operation, **fields):
        calls.append(operation)
        if operation == "capabilities":
            return {"protocol": 1, "identity": "actomasto-v1", "build_revision": "b" * 40,
                    "local_only": True, "metadata_only": True, "revision_bound": True,
                    "snapshot_enumeration": True, "coverage_freshness": True, "harnesses": {"omp": VERSIONS["omp"]}}
        if operation == "enumerate":
            return {"sources": [{"harness": "omp", "id": "c" * 64, "state": "present", "revision": "revision-one"}],
                    "snapshot": "snapshot", "scope_id": "scope", "coverage": coverage,
                    "refreshed_at": STAMP, "lag_seconds": 0, "next_cursor": None}
        if operation == "turns":
            return {"turns": [copy.deepcopy(metadata)], "status": status, "pending_turns": 0, "next_offset": None}
        if operation == "read":
            return {"turn": copy.deepcopy(turn)}
        raise AssertionError("Unexpected Funes mutation or operation")
    monkeypatch.setattr(FunesSource, "request", request)
    monkeypatch.setattr("actomasto.funes_source.time.time", lambda: STAMP)
    return calls


def conversation_config(root, **extra):
    return config(root, funes={"executable": "/test/funes", "corpus": "/test/corpus", "scope": "/test/scope"},
                  conversation_harnesses=["omp"], **extra)


def test_explicit_enrollment_reads_complete_turns_with_original_provenance(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"}, stamp=STAMP - 40 * 86400)
    calls = install_funes_protocol(monkeypatch, repo.path)
    assert evidence(sources.collect(config(tmp_path), now=NOW), "conversation") == []
    assert calls == []
    bundle = sources.collect(conversation_config(tmp_path), now=NOW)
    items = sorted(evidence(bundle, "conversation"), key=lambda item: item["time"])
    assert [item["provenance"] for item in items] == ["user_reported", "assistant_reported"]
    assert items[0]["text"] == "Defer caching. Next inspect eviction boundaries."
    assert items[1]["text"] == "I reported the checks passing; not independently verified."
    assert items[0]["source"] == "omp session session-one message user-one"
    assert bundle["projects"][0]["last_activity"] == sources._iso(STAMP - 3599)
    assert "read" in calls and not {"refresh", "bind", "enroll"}.intersection(calls)


def test_stale_conversations_cannot_be_used(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    install_funes_protocol(monkeypatch, repo.path, coverage="lagging")
    bundle = sources.collect(conversation_config(tmp_path), now=NOW)
    assert evidence(bundle, "conversation") == []
    assert evidence(bundle, "commit")
    assert any("conversations unavailable" in note for note in bundle["coverage"])


def test_complete_turns_survive_unrelated_pending_stream(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    install_funes_protocol(monkeypatch, repo.path, status="incomplete_turn")
    bundle = sources.collect(conversation_config(tmp_path), now=NOW)
    assert {item["provenance"] for item in evidence(bundle, "conversation")} == {"user_reported", "assistant_reported"}
    assert any("partial coverage" in note for note in bundle["coverage"])


def test_limit_preserves_individually_complete_turn(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    install_funes_protocol(monkeypatch, repo.path, user_text="Defer caching.", assistant_text="Reported.")
    monkeypatch.setattr(sources, "MAX_CONVERSATION_BYTES", len("Defer caching.Reported.".encode()))
    bundle = sources.collect(conversation_config(tmp_path), now=NOW)
    assert {item["text"] for item in evidence(bundle, "conversation")} == {"Defer caching.", "Reported."}
    assert any("complete turns retained with incomplete coverage" in note for note in bundle["coverage"])


def test_complete_turn_policy_veto_drops_user_and_assistant_together(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    install_funes_protocol(monkeypatch, repo.path, assistant_text="PRIVATE TOPIC continuation")
    bundle = sources.collect(conversation_config(tmp_path, blocklist={"text": ["PRIVATE TOPIC"]}), now=NOW)
    assert evidence(bundle, "conversation") == []
    assert "Defer caching" not in json.dumps(bundle)


def test_unsupported_harness_does_not_discard_healthy_harness_or_git(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    install_funes_protocol(monkeypatch, repo.path)
    settings = conversation_config(tmp_path)
    settings["conversation_harnesses"] = ["codex", "omp"]
    bundle = sources.collect(settings, now=NOW)
    assert evidence(bundle, "commit")
    assert {item["provenance"] for item in evidence(bundle, "conversation")} == {"user_reported", "assistant_reported"}
    assert any("codex conversations unavailable" in note for note in bundle["coverage"])


def test_source_work_and_size_limits_report_incomplete_coverage(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"})
    monkeypatch.setattr(sources, "MAX_GIT_CALLS", 1)
    bundle = sources.collect(config(tmp_path), now=NOW)
    assert evidence(bundle, "commit") == []
    assert any("limit" in note or "bound" in note for note in bundle["coverage"])


def test_idle_repository_prose_does_not_displace_work(tmp_path):
    idle = Repository(tmp_path / "aaa-idle", origin="https://github.com/owner/idle.git")
    idle.commit({"README.md": "Speculative roadmap\n" * 1000}, stamp=STAMP - 45 * 86400)
    active = Repository(tmp_path / "zzz-active", origin="https://github.com/owner/active.git")
    active.commit({"main.py": "x = 1"}, summary="Actual recent work")
    bundle = sources.collect(config(tmp_path), now=NOW)
    assert [project["id"] for project in bundle["projects"]] == ["git:github.com/owner/active"]
    assert "Speculative roadmap" not in json.dumps(bundle)
    reentry = sources.collect(config(tmp_path), now=NOW, project="idle")
    assert [project["id"] for project in reentry["projects"]] == ["git:github.com/owner/idle"]
    assert evidence(reentry, "planning")[0]["provenance"] == "observed"


def test_reentry_name_retains_differently_named_worktree(tmp_path):
    repo = Repository(tmp_path / "canonical", origin="https://github.com/owner/application.git")
    repo.commit({"main.py": "x = 1"}, stamp=STAMP - 45 * 86400)
    worktree = tmp_path / "fix-unrelated-name"
    repo.run("worktree", "add", "-b", "fix", str(worktree))
    (worktree / "main.py").write_text("x = 2")
    bundle = sources.collect(config(tmp_path), now=NOW, project="canonical")
    assert len(bundle["projects"]) == 1 and bundle["projects"][0]["name"] == "application"
    assert any(str(worktree) in item["source"] and "uncommitted" in item["text"]
               for item in evidence(bundle, "working_tree"))
    assert evidence(bundle, "commit")[0]["time"] == sources._iso(STAMP - 45 * 86400)
    assert str(worktree) in evidence(bundle, "commit")[0]["source"]


def test_ambiguous_project_name_is_not_silently_merged(tmp_path):
    one = Repository(tmp_path / "one", origin="https://github.com/first/application.git")
    two = Repository(tmp_path / "two", origin="https://github.com/second/application.git")
    one.commit({"main.py": "x = 1"})
    two.commit({"main.py": "x = 2"})
    with pytest.raises(sources.BriefingSourceError, match="ambiguous_project"):
        sources.collect(config(tmp_path), now=NOW, project="application")


def test_shallow_projects_precede_deep_sibling_when_discovery_is_capped(tmp_path, monkeypatch):
    deep = tmp_path / "aaa-deep"
    for _ in range(15):
        deep /= "internal"
    deep.mkdir(parents=True)
    target = Repository(tmp_path / "zzz-target")
    target.commit({"main.py": "x = 1"}, summary="Later sibling authored work")
    monkeypatch.setattr(sources, "MAX_DIRECTORIES", 8)
    bundle = sources.collect(config(tmp_path), now=NOW)
    assert evidence(bundle, "commit")[0]["text"].startswith("Later sibling authored work")
    assert any("discovery capped" in note for note in bundle["coverage"])


def test_author_filter_precedes_commit_count_limit(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"ours.py": "x = 1"}, summary="Personal history behind upstream churn")
    for index in range(4):
        repo.commit({f"foreign{index}.py": "x = 1"}, email="foreign@example.test",
                    summary="Upstream commit", stamp=STAMP - 99 + index)
    monkeypatch.setattr(sources, "MAX_COMMITS", 2)
    bundle = sources.collect(config(tmp_path), now=NOW)
    commits = evidence(bundle, "commit")
    assert len(commits) == 1
    assert commits[0]["text"].startswith("Personal history behind upstream churn")


def test_reentry_recovers_old_authored_history_without_widening_daily(tmp_path):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"}, summary="Last recorded work", stamp=STAMP - 45 * 86400)
    unrelated = Repository(tmp_path / "unrelated", origin="https://github.com/owner/unrelated.git")
    unrelated.commit({"other.py": "x = 1"}, summary="Unrelated old work", stamp=STAMP - 50 * 86400)
    assert evidence(sources.collect(config(tmp_path), now=NOW), "commit") == []
    bundle = sources.collect(config(tmp_path), now=NOW, project="project")
    assert [item["text"].splitlines()[0] for item in evidence(bundle, "commit")] == ["Last recorded work"]
    assert bundle["projects"][0]["last_activity"] == sources._iso(STAMP - 45 * 86400)
    assert "Unrelated old work" not in json.dumps(bundle)


def test_reentry_can_recover_old_complete_recorded_decisions(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "repo")
    repo.commit({"main.py": "x = 1"}, stamp=STAMP - 50 * 86400)
    install_funes_protocol(monkeypatch, repo.path, age_days=45)
    settings = conversation_config(tmp_path)
    assert evidence(sources.collect(settings, now=NOW), "conversation") == []
    bundle = sources.collect(settings, now=NOW, project="project")
    items = evidence(bundle, "conversation")
    assert {item["provenance"] for item in items} == {"user_reported", "assistant_reported"}
    assert next(item for item in items if item["provenance"] == "user_reported")["time"] == sources._iso(STAMP - 3600 - 45 * 86400)
