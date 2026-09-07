"""Synthetic local Git objects and mock public APIs; no provider/remote calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest

from actomasto.discovery import discover, normalize_origin, verify
from actomasto import git_source
from actomasto.common import SourceCancelled

REPOSITORY = {"id": "github.com:123", "display_path": "github.com/example/project"}
TIME = 1_700_000_000


class GitFixture:
    def __init__(self, path):
        self.path = path
        path.mkdir(parents=True)
        self.run("init", "--initial-branch=main", "--template=")
        self.head = None
        self.sequence = 0

    def run(self, *args, data=None, timestamp=TIME):
        environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                       "GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": "author@example.test",
                       "GIT_COMMITTER_NAME": "Fixture Committer", "GIT_COMMITTER_EMAIL": "other@example.test",
                       "GIT_AUTHOR_DATE": f"{timestamp - 60} +0000", "GIT_COMMITTER_DATE": f"{timestamp} +0000"}
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.path), *args],
                              input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=environment).stdout

    def commit(self, files, *, parents=None, message="Implement fixture behavior", ref="refs/heads/main", email="author@example.test", timestamp=None):
        if parents is None:
            parents = [self.head] if self.head else []
        self.run("read-tree", parents[0] if parents else "--empty")
        for name, content in files.items():
            if content is None:
                self.run("update-index", "--force-remove", "--", name)
                continue
            if isinstance(content, tuple):
                mode, oid = content
            else:
                mode = "100644"
                oid = self.run("hash-object", "-w", "--stdin", data=content.encode() if isinstance(content, str) else content).decode().strip()
            self.run("update-index", "--add", "--cacheinfo", mode, oid, name)
        tree = self.run("write-tree").decode().strip()
        self.sequence += 1
        # commit-tree accepts explicit identity through environment overrides.
        command = ["git", "-C", str(self.path), "-c", "core.hooksPath=/dev/null", "commit-tree", tree]
        for parent in parents:
            command.extend(("-p", parent))
        when = timestamp if timestamp is not None else TIME + self.sequence
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": "Not The Author", "GIT_COMMITTER_EMAIL": "other@example.test",
               "GIT_AUTHOR_DATE": f"{TIME - 86400} +0000", "GIT_COMMITTER_DATE": f"{when} +0000"}
        oid = subprocess.run(command, input=message.encode(), env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=True).stdout.decode().strip()
        if ref:
            self.run("update-ref", ref, oid)
        if ref == "refs/heads/main":
            self.head = oid
        return oid

    def units(self, eligible=lambda start, end: True):
        return list(git_source.collect(REPOSITORY, str(self.path), [" AUTHOR@EXAMPLE.TEST "], eligible))


@pytest.fixture
def repository(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return GitFixture(tmp_path / "roots" / "repo")


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/Owner/Project.git", "github.com/owner/project"),
    ("git@github.com:Owner/Project.git", "github.com/owner/project"),
    ("ssh://git@gitlab.com/group/subgroup/project.git", "gitlab.com/group/subgroup/project"),
    ("git@gitlab-ssh.wikimedia.org:repos/team/project.git", "gitlab.wikimedia.org/repos/team/project"),
    ("ssh://git@gitlab-ssh.wikimedia.org:22/repos/team/project.git", "gitlab.wikimedia.org/repos/team/project"),
])
def test_normalize_supported_origins(url, expected):
    assert normalize_origin(url) == expected


@pytest.mark.parametrize("url", [
    "https://token@github.com/team/repo", "https://user:password@github.com/team/repo",
    "ssh://git:password@github.com/team/repo", "ext::execute", "file:///tmp/repo",
    "https://unsupported.example/team/repo", "https://github.com/team/repo?secret=value",
    "https://github.com/team/%2e%2e", "git@github.com:team/../repo", "https://github.com/team/repo#fragment",
])
def test_untrusted_or_ambiguous_origins_fail_closed(url):
    with pytest.raises(ValueError):
        normalize_origin(url)


@pytest.mark.parametrize("origin, payload, endpoint, identity", [
    ("github.com/team/repo", {"id": 12, "private": False}, "https://api.github.com/repos/team/repo", "github.com:12"),
    ("gitlab.com/team/sub/repo", {"id": 34, "visibility": "public"}, "https://gitlab.com/api/v4/projects/team%2Fsub%2Frepo", "gitlab.com:34"),
    ("gitlab.wikimedia.org/repos/team/project", {"id": 56, "visibility": "public"}, "https://gitlab.wikimedia.org/api/v4/projects/repos%2Fteam%2Fproject", "gitlab.wikimedia.org:56"),
])
def test_public_checks_use_anonymous_host_api(origin, payload, endpoint, identity):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify(origin, client)
    assert result["state"] == "public"
    assert result["id"] == identity
    assert str(requests[0].url) == endpoint
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("status, payload, expected", [
    (200, {"id": 1, "private": True}, "private"),
    (200, {"id": 1, "private": "false"}, "uncertain"),
    (200, {"id": True, "private": False}, "uncertain"),
    (200, {"private": False}, "uncertain"),
    (401, {}, "uncertain"), (403, {}, "uncertain"), (404, {}, "uncertain"), (429, {}, "uncertain"),
])
def test_visibility_requires_explicit_typed_public_response(status, payload, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))) as client:
        assert verify("github.com/team/repo", client)["state"] == expected


def test_redirect_never_authorizes_or_sends_second_request():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/private"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        assert verify("github.com/team/repo", client)["state"] == "uncertain"
    assert len(requests) == 1


def test_visibility_honors_rate_limit_and_bounds_response(monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "7200"}))) as client:
        assert verify("github.com/team/repo", client)["retry_after"] == 7200
    monkeypatch.setattr("actomasto.discovery.MAX_RESPONSE_BYTES", 64)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 65))) as client:
        assert verify("github.com/team/repo", client)["reason"] == "visibility_oversized"


def test_origin_is_authoritative_and_conflicts_fail_closed(repository):
    repository.run("remote", "add", "upstream", "https://github.com/public/upstream.git")
    assert discover([repository.path.parent])[0]["reason"] == "missing_origin"
    repository.run("remote", "add", "origin", "git@gitlab.com:private/project.git")
    result = discover([repository.path.parent])[0]
    assert result["origin"] == "gitlab.com/private/project"
    repository.run("config", "--add", "remote.origin.url", "https://github.com/public/upstream.git")
    assert discover([repository.path.parent])[0]["reason"] == "conflicting_origin"


def test_discovery_nested_worktrees_symlinks_and_bare(repository, tmp_path):
    repository.commit({"file.txt": "base\n"})
    repository.run("remote", "add", "origin", "git@github.com:team/repo.git")
    nested = GitFixture(repository.path / "nested")
    nested.run("remote", "add", "origin", "https://gitlab.com/group/project.git")
    worktree = repository.path.parent / "linked"
    repository.run("worktree", "add", "-b", "linked", str(worktree))
    outside = GitFixture(tmp_path / "outside")
    (repository.path.parent / "escape").symlink_to(outside.path, target_is_directory=True)
    bare = repository.path.parent / "bare"
    repository.run("init", "--bare", "--template=", str(bare))
    results = discover([repository.path.parent])
    paths = {result["path"] for result in results}
    assert paths == {str(repository.path), str(nested.path), str(worktree), str(bare)}
    assert next(result for result in results if result["path"] == str(bare))["reason"] == "bare_repository"
    origins = [result["origin"] for result in results]
    assert origins.count("github.com/team/repo") == 2


def test_all_ref_namespaces_authors_unpushed_and_no_reflog_history(repository):
    base = repository.commit({"base.txt": "base\n"})
    branch = repository.commit({"branch.txt": "local unpushed work\n"}, parents=[base], ref="refs/heads/topic")
    remote = repository.commit({"remote.txt": "remote tracking\n"}, parents=[base], ref="refs/remotes/origin/topic")
    tagged = repository.commit({"tag.txt": "tag only\n"}, parents=[base], ref="refs/tags/lightweight")
    annotated = repository.commit({"annotated.txt": "annotated tag only\n"}, parents=[base], ref=None)
    repository.run("-c", "user.name=Fixture", "-c", "user.email=author@example.test", "tag", "-a", "annotated", annotated, "-m", "fixture annotation")
    repository.run("-c", "user.name=Fixture", "-c", "user.email=author@example.test", "tag", "-a", "tag-chain", "annotated", "-m", "fixture nested annotation")
    tree = repository.run("rev-parse", base + "^{tree}").decode().strip()
    repository.run("tag", "tree-only", tree)
    wrong = repository.commit({"wrong.txt": "other author\n"}, parents=[base], email="other@example.test", ref="refs/heads/other")
    dangling = repository.commit({"dangling.txt": "not reachable\n"}, parents=[base], ref="refs/heads/temporary")
    repository.run("update-ref", "-d", "refs/heads/temporary")
    commits = {unit["source_ref"]["commit"] for unit in repository.units()}
    assert commits == {base, branch, remote, tagged, annotated}
    assert wrong not in commits and dangling not in commits


def test_committer_time_is_authorization_time_and_future_waits(repository):
    accepted = repository.commit({"a": "a"}, timestamp=TIME + 10)
    excluded = repository.commit({"b": "b"}, timestamp=TIME + 20)
    future = repository.commit({"c": "c"}, timestamp=4_000_000_000)
    checked = []
    def eligible(start, end):
        checked.append((start, end))
        return start < TIME + 20
    units = repository.units(eligible)
    assert {unit["source_ref"]["commit"] for unit in units} == {accepted}
    assert checked == [(TIME + 10, TIME + 10), (TIME + 20, TIME + 20)]
    assert excluded != future


def test_committed_ignore_and_parent_exclusion_reject_complete_commit(repository):
    oid = repository.commit({".gitignore": "private/\n!private/public.txt\n", "private/public.txt": "not eligible\n", "safe.txt": "safe\n"}, message="Must also disappear")
    # Deliberately contradictory uncommitted ignore contents cannot authorize it.
    (repository.path / ".gitignore").write_text("!private/\n!private/public.txt\n")
    unit = repository.units()[0]
    assert unit["source_ref"] == {"commit": oid}
    assert unit["exclusion_reason"] == "ignored_path"
    assert unit["items"] == [] and unit["paths"] == []


def test_nested_negation_and_local_metadata_exclusions(repository):
    allowed = repository.commit({".gitignore": "*.log\n", "sub/.gitignore": "!keep.log\n", "sub/keep.log": "eligible fixture\n"})
    assert next(unit for unit in repository.units() if unit["source_ref"]["commit"] == allowed)["items"]
    (repository.path / ".git" / "info").mkdir(exist_ok=True)
    (repository.path / ".git" / "info" / "exclude").write_text("sub/keep.log\n")
    assert repository.units()[0]["exclusion_reason"] == "ignored_path"


def test_global_exclusion_metadata_is_applied(repository, tmp_path):
    repository.commit({"local-only.txt": "do not collect\n"})
    excludes = tmp_path / "global-ignore"
    excludes.write_text("local-only.txt\n")
    repository.run("config", "core.excludesFile", str(excludes))
    assert repository.units()[0]["exclusion_reason"] == "ignored_path"


@pytest.mark.parametrize("name, contents", [
    (".env.local", "PUBLIC_NAME=fixture\n"), ("private.key", "synthetic\n"),
    ("credentials.json", "{}"), ("ordinary.json", '{"type":"service_account","project_id":"synthetic"}'),
])
def test_sensitive_path_or_service_account_excludes_message_and_safe_files(repository, name, contents):
    repository.commit({name: contents, "safe.txt": "eligible alone"}, message="This message must not escape")
    unit = repository.units()[0]
    assert unit["exclusion_reason"] == "blocked_path"
    assert unit["items"] == [] and unit["paths"] == []


def test_rename_out_of_sensitive_path_is_still_whole_commit_excluded(repository):
    root = repository.commit({".env": "fixture\n"})
    renamed = repository.commit({".env": None, "public.txt": "fixture\n"})
    units = {unit["source_ref"]["commit"]: unit for unit in repository.units()}
    assert units[root]["exclusion_reason"] == "blocked_path"
    assert units[renamed]["exclusion_reason"] == "blocked_path"


def test_placeholder_is_eligible_but_binary_lfs_submodule_and_symlink_payloads_are_not(repository):
    base = repository.commit({"base": "base"})
    symlink_oid = repository.run("hash-object", "-w", "--stdin", data=b"/synthetic/private/location").decode().strip()
    oid = repository.commit({".env.example": "EXAMPLE_NAME=sample\n", "binary.dat": b"\0private_binary_fixture",
                             "large.dat": "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 123\n",
                             "module": ("160000", base), "link": ("120000", symlink_oid)})
    unit = next(unit for unit in repository.units() if unit["source_ref"]["commit"] == oid)
    text = "\n".join(item["text"] for item in unit["items"])
    assert "EXAMPLE_NAME=sample" in text
    assert "private_binary_fixture" not in text and "sha256:" not in text and "/synthetic/private/location" not in text
    assert all(category in text for category in ("binary", "lfs", "submodule", "symlink"))


def test_root_empty_and_merge_resolution_use_correct_diff_semantics(repository):
    root = repository.commit({"shared.txt": "base\n"})
    left = repository.commit({"shared.txt": "left\n", "left.txt": "left-only\n"})
    right = repository.commit({"shared.txt": "right\n", "right.txt": "right-only\n"}, parents=[root], ref="refs/heads/right")
    merge = repository.commit({"shared.txt": "resolved\n", "right.txt": "right-only\n"}, parents=[left, right], message="Resolve two approaches")
    empty = repository.commit({}, message="Document a design decision")
    units = {unit["source_ref"]["commit"]: unit for unit in repository.units()}
    assert "+base" in "\n".join(item["text"] for item in units[root]["items"])
    merge_text = "\n".join(item["text"] for item in units[merge]["items"])
    assert "diff --cc shared.txt" in merge_text and "resolved" in merge_text
    assert "left-only" not in merge_text and "right-only" not in merge_text
    assert [item["text"] for item in units[empty]["items"]] == ["Document a design decision"]


def test_equivalent_rebase_identity_changes_with_message_or_patch(repository):
    base = repository.commit({"code.txt": "one\n"})
    original = repository.commit({"code.txt": "two\n"}, message="Improve behavior")
    new_base = repository.commit({"unrelated.txt": "other change\n"}, parents=[base], ref="refs/heads/newbase")
    rebased = repository.commit({"code.txt": "two\n"}, parents=[new_base], ref="refs/heads/rebased", message="Improve behavior")
    renamed_message = repository.commit({"code.txt": "two\n"}, parents=[base], ref="refs/heads/message", message="Different explanation")
    changed_patch = repository.commit({"code.txt": "three\n"}, parents=[base], ref="refs/heads/patch", message="Improve behavior")
    units = {unit["source_ref"]["commit"]: unit for unit in repository.units()}
    assert units[original]["id"] != units[rebased]["id"]
    assert units[original]["equivalent_id"] == units[rebased]["equivalent_id"]
    assert units[original]["equivalent_id"] != units[renamed_message]["equivalent_id"]
    assert units[original]["equivalent_id"] != units[changed_patch]["equivalent_id"]


def test_oversized_commit_is_content_free_and_terminal(repository, monkeypatch):
    oid = repository.commit({"large.txt": "synthetic data\n" * 1000})
    monkeypatch.setattr(git_source, "MAX_SOURCE_BYTES", 1024)
    unit = repository.units()[0]
    assert unit["source_ref"] == {"commit": oid}
    assert unit["exclusion_reason"] == "oversized_source"
    assert unit["items"] == [] and unit["paths"] == []


@pytest.mark.parametrize("code, operation", [
    ("git_timeout", ("cat-file", "commit")),
    ("git_unavailable", ("ls-tree",)),
    ("git_read_failed", ("cat-file", "blob")),
])
def test_transient_commit_read_failure_retries_without_terminal_exclusion(repository, monkeypatch, code, operation):
    oid = repository.commit({"code.txt": "retryable committed content\n"})
    original = git_source._git
    failed = False
    def transient(path, *args, **kwargs):
        nonlocal failed
        if not failed and args[:len(operation)] == operation:
            failed = True
            raise git_source.GitSourceError(code)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(git_source, "_git", transient)
    with pytest.raises(git_source.GitSourceError) as caught:
        repository.units()
    assert caught.value.code == code
    unit, = repository.units()
    assert unit["source_ref"] == {"commit": oid}
    assert "exclusion_reason" not in unit
    assert any("retryable committed content" in item["text"] for item in unit["items"])


def test_cancellation_after_eligibility_prevents_next_git_process(repository, monkeypatch):
    repository.commit({"code.txt": "must not read diff\n"})
    disabled = False
    forbidden_processes = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        if disabled:
            forbidden_processes.append(args[0])
        return original(*args, **kwargs)
    def eligible(start, end):
        nonlocal disabled
        disabled = True
        return True
    def cancelled():
        if disabled:
            raise SourceCancelled()
    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(SourceCancelled):
        list(git_source.collect(REPOSITORY, str(repository.path), ["author@example.test"], eligible, cancelled))
    assert forbidden_processes == []


def test_cancellation_stops_pipe_reads_and_reaps_git_process(repository, monkeypatch):
    repository.commit({"code.txt": "must not read stdout\n"})
    processes = []
    forbidden_reads = []
    original_launch = subprocess.Popen
    original_read = os.read
    def launch(*args, **kwargs):
        process = original_launch(*args, **kwargs)
        processes.append(process)
        return process
    def read(fd, size):
        if processes:
            forbidden_reads.append(fd)
        return original_read(fd, size)
    def cancelled():
        if processes:
            raise SourceCancelled()
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(SourceCancelled):
        list(git_source.collect(REPOSITORY, str(repository.path), ["author@example.test"], lambda *args: True, cancelled))
    assert forbidden_reads == []
    assert len(processes) == 1 and processes[0].returncode is not None


def test_cancellation_prevents_local_exclusion_metadata_open(repository, monkeypatch):
    repository.commit({"code.txt": "committed fixture\n"})
    disabled = False
    forbidden_opens = []
    original_metadata = git_source._metadata_file
    original_open = os.open
    def metadata(path):
        nonlocal disabled
        disabled = True
        return original_metadata(path)
    def open_file(*args, **kwargs):
        if disabled:
            forbidden_opens.append(args[0])
        return original_open(*args, **kwargs)
    def cancelled():
        if disabled:
            raise SourceCancelled()
    monkeypatch.setattr(git_source, "_metadata_file", metadata)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(SourceCancelled):
        list(git_source.collect(REPOSITORY, str(repository.path), ["author@example.test"], lambda *args: True, cancelled))
    assert forbidden_opens == []


def test_interleaved_collectors_do_not_share_cancellation(repository):
    first_oid = repository.commit({"first.txt": "first\n"})
    second_oid = repository.commit({"second.txt": "second\n"})
    disabled = False
    def cancelled():
        if disabled:
            raise SourceCancelled()
    first = git_source.collect(REPOSITORY, str(repository.path), ["author@example.test"], lambda *args: True, cancelled)
    second = git_source.collect(REPOSITORY, str(repository.path), ["author@example.test"], lambda *args: True)
    try:
        assert next(first)["source_ref"] == {"commit": first_oid}
        disabled = True
        assert git_source._git(repository.path, "rev-parse", "HEAD").strip().decode() == second_oid
        assert next(second)["source_ref"] == {"commit": first_oid}
        with pytest.raises(SourceCancelled):
            next(first)
        assert next(second)["source_ref"] == {"commit": second_oid}
        with pytest.raises(StopIteration):
            next(second)
    finally:
        first.close()
        second.close()


def test_collection_never_runs_hooks_helpers_textconv_or_reads_worktree_diff(repository):
    oid = repository.commit({"code.txt": "committed fixture\n", ".gitattributes": "*.txt diff=hostile\n"})
    marker = repository.path.parent / "executed"
    script = repository.path.parent / "hostile"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\necho forbidden_external_output\n")
    script.chmod(0o700)
    repository.run("config", "diff.hostile.textconv", str(script))
    repository.run("config", "diff.external", str(script))
    repository.run("config", "core.fsmonitor", str(script))
    repository.run("config", "core.hooksPath", str(script))
    repository.run("config", "remote.origin.url", "ext::" + str(script))
    (repository.path / "code.txt").write_text("uncommitted fixture must never appear\n")
    (repository.path / ".gitattributes").write_text("*.txt -diff\n")
    unit = next(unit for unit in repository.units() if unit["source_ref"]["commit"] == oid)
    text = "\n".join(item["text"] for item in unit["items"])
    assert "+committed fixture" in text
    assert "uncommitted fixture" not in text and "forbidden_external_output" not in text
    assert not marker.exists()


def test_escaped_credential_schema_cannot_bypass_whole_commit_exclusion(repository):
    repository.commit({"ordinary.json": r'{"type":"\u0073ervice_account","project_id":"fixture"}'})
    unit = repository.units()[0]
    assert unit["exclusion_reason"] == "blocked_path"
    assert unit["items"] == [] and unit["paths"] == []


def test_double_star_ignore_and_deleting_ignore_file_do_not_reauthorize_old_path(repository):
    repository.commit({".gitignore": "cache/**/generated?.txt\n", "cache/deep/generated1.txt": "fixture\n"})
    deleted_rule = repository.commit({".gitignore": None, "cache/deep/generated1.txt": None})
    units = {unit["source_ref"]["commit"]: unit for unit in repository.units()}
    assert units[deleted_rule]["exclusion_reason"] == "ignored_path"


def test_gitlab_internal_and_network_failure_are_distinct():
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": 123, "visibility": "internal"})
    )) as client:
        assert verify("gitlab.com/team/repo", client)["state"] == "private"
    def disconnected(request):
        raise httpx.ConnectError("synthetic unavailable", request=request)
    with httpx.Client(transport=httpx.MockTransport(disconnected)) as client:
        result = verify("gitlab.com/team/repo", client)
        assert result["state"] == "uncertain"
        assert result["id"] is None
