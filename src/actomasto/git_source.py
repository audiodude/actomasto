"""Bounded, read-only committed-object collection; never fetch or invoke project code."""
from __future__ import annotations

from contextlib import ExitStack
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time

from pathspec import GitIgnoreSpec

from .policy import sensitive_path

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
ADAPTER_VERSION = "git-1"
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_cancelled = ContextVar("git_source_cancelled", default=None)


def _check_cancelled():
    callback = _cancelled.get()
    if callback is not None:
        callback()


class GitSourceError(Exception):
    """Content-free repository/source failure."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _git(path, *args, limit=MAX_METADATA_BYTES, input_data=None, allowed=(0,), attr_source=None, global_config=False):
    # Whitelist environment: inherited GIT_DIR, config injection, alternates,
    # credential helpers, pagers, and external-diff settings must not leak in.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME") if key in os.environ}
    env.update({"LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
                "GIT_ATTR_NOSYSTEM": "1", "GIT_PAGER": "cat", "GIT_LITERAL_PATHSPECS": "1",
                "GIT_ALLOW_PROTOCOL": ""})
    if not global_config:
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    if attr_source:
        env["GIT_ATTR_SOURCE"] = attr_source
    command = ["git", "--no-pager", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=never",
               "-c", "diff.external=", "-c", "diff.trustExitCode=false",
               "-c", "diff.ignoreSubmodules=none", "-c", "submodule.recurse=false",
               "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "core.pager=cat",
               "-c", "core.quotePath=true", "-C", str(path)]
    if args and args[0] == "diff-tree":
        git_dir = _text(_git(path, "rev-parse", "--absolute-git-dir")).strip()
        command.extend(("--git-dir", git_dir, "-c", "core.bare=true"))
    command.extend(args)
    output = bytearray()
    try:
        with ExitStack() as stack:
            if args and args[0] == "diff-tree":
                # An empty index and bare setup prevent diff attribute lookup
                # from reading either working files or staged replacements.
                temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="actomasto-git-"))
                env["GIT_INDEX_FILE"] = os.path.join(temporary, "absent-index")
            _check_cancelled()
            process = stack.enter_context(subprocess.Popen(
                command, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env))
            try:
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(process.stdout.fileno(), False)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    position = 0
                    if input_data is not None:
                        os.set_blocking(process.stdin.fileno(), False)
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    deadline = time.monotonic() + 30
                    while selector.get_map():
                        _check_cancelled()
                        if time.monotonic() >= deadline:
                            raise GitSourceError("git_timeout")
                        for key, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                            _check_cancelled()
                            if key.fileobj is process.stdin:
                                try:
                                    position += os.write(key.fd, input_data[position:position + 65536])
                                except BrokenPipeError:
                                    position = len(input_data)
                                if position == len(input_data):
                                    selector.unregister(key.fileobj)
                                    process.stdin.close()
                            else:
                                chunk = os.read(key.fd, min(65536, max(1, limit + 1 - len(output))))
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                else:
                                    output.extend(chunk)
                                    if len(output) > limit:
                                        raise GitSourceError("oversized_source")
                    try:
                        code = process.wait(timeout=max(.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        raise GitSourceError("git_timeout") from None
                    if code not in allowed:
                        raise GitSourceError("git_read_failed")
            except BaseException:
                process.kill()
                process.wait()
                raise
    except OSError:
        raise GitSourceError("git_unavailable") from None
    return bytes(output)


def _digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _text(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise GitSourceError("unsupported_source_encoding") from None


def _sensitive(path):
    return any(sensitive_path(name) for name in path.split("/"))


def _ignore_spec(data):
    try:
        return GitIgnoreSpec.from_lines(_text(data).splitlines())
    except ValueError:
        raise GitSourceError("invalid_ignore_metadata") from None


def _metadata_file(path):
    # Metadata only, and bounded even when an exclusion file is a special file.
    _check_cancelled()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return b""
    except OSError:
        raise GitSourceError("ignore_metadata_unreadable") from None
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise GitSourceError("ignore_metadata_unreadable")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            _check_cancelled()
            value = stream.read(MAX_METADATA_BYTES + 1)
        if len(value) > MAX_METADATA_BYTES:
            raise GitSourceError("oversized_source")
        return value
    finally:
        os.close(fd)


def _local_ignores(path):
    common = _text(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()
    sources = [_metadata_file(Path(common) / "info" / "exclude")]
    configured = _git(path, "config", "--path", "--get", "core.excludesFile", allowed=(0, 1), global_config=True)
    if configured:
        exclude = Path(_text(configured).strip()).expanduser()
        if not exclude.is_absolute():
            exclude = Path(path) / exclude
    else:
        exclude = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "ignore"
    sources.append(_metadata_file(exclude))
    # Conservatively keep each metadata source as a separate veto: an unrelated
    # negation in another source cannot accidentally authorize local exclusions.
    return [_ignore_spec(value) for value in sources]


def _tree(path, revision, budget):
    raw = _git(path, "ls-tree", "-r", "-z", "--full-tree", revision, limit=budget[0])
    budget[0] -= len(raw)
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        name = _text(name)
        if any(ord(char) < 32 or ord(char) == 127 for char in name) or any(part in ("", ".", "..") for part in name.split("/")):
            raise GitSourceError("unsupported_source_path")
        entries[name] = (mode, kind, oid)
    return entries


def _blob(path, oid, budget, cache):
    if oid in cache:
        return cache[oid]
    size = int(_git(path, "cat-file", "-s", oid, limit=128).strip())
    if size > budget[0]:
        raise GitSourceError("oversized_source")
    data = _git(path, "cat-file", "blob", oid, limit=budget[0])
    budget[0] -= len(data)
    cache[oid] = data
    return data


def _ignored(name, tree, path, budget, blobs, specs, local):
    parts = name.split("/")
    active = []
    # Test every parent directory before descending: Git cannot unignore a child
    # of an excluded directory, even with a more-specific negation.
    for depth in range(len(parts)):
        base = "/".join(parts[:depth])
        ignore_name = (base + "/" if base else "") + ".gitignore"
        entry = tree.get(ignore_name)
        if entry:
            if entry[0] not in ("100644", "100755") or entry[1] != "blob":
                raise GitSourceError("unsupported_ignore_metadata")
            oid = entry[2]
            if oid not in specs:
                specs[oid] = _ignore_spec(_blob(path, oid, budget, blobs))
            active.append((base, specs[oid]))
        candidate = "/".join(parts[:depth + 1]) + ("/" if depth < len(parts) - 1 else "")
        if any(spec.match_file(candidate) for spec in local):
            return True
        ignored = False
        for scope, spec in active:
            relative = candidate[len(scope) + 1:] if scope else candidate
            match = spec.check_file(relative)
            if match.include is not None:
                ignored = bool(match.include)
        if ignored:
            return True
    return False


def _changes(path, commit, parents, budget):
    # -m union guards *all* parent-side paths, even if a merge's combined diff
    # omits them. No rename detection means both rename sides remain guarded.
    raw = _git(path, "diff-tree", "--root", "-m", "-r", "--no-commit-id", "--raw", "-z",
               "--no-renames", "--no-ext-diff", "--no-textconv", commit, "--", limit=budget[0], attr_source=commit)
    budget[0] -= len(raw)
    parts = raw.split(b"\0")
    paths = set()
    for index in range(0, len(parts) - 1, 2):
        if not parts[index].startswith(b":"):
            raise GitSourceError("malformed_git_metadata")
        paths.add(_text(parts[index + 1]))
    return sorted(paths)


def _patch(path, commit, name, budget):
    patch = _git(path, "diff-tree", "--root", "-r", "--cc", "--no-commit-id", "-p", "--no-renames",
                 "--no-ext-diff", "--no-textconv", "--no-color", "--full-index", "--unified=3",
                 "--src-prefix=a/", "--dst-prefix=b/", commit, "--", name,
                 limit=budget[0], attr_source=commit)
    budget[0] -= len(patch)
    return patch


def _unit(repo, commit, event_time):
    identity = _digest(repo["id"], commit)
    return {"id": identity, "repository_id": repo["id"], "kind": "commit", "event_time": event_time,
            "event_end": event_time, "equivalent_id": None, "source_ref": {"commit": commit}, "paths": [],
            "items": [], "adapter": "git", "adapter_version": ADAPTER_VERSION, "partial_source": False}


def _reachable(path):
    refs = _git(path, "for-each-ref", "--format=%(objectname) %(objecttype)",
                "refs/heads/", "refs/remotes/", "refs/tags/")
    tips = set()
    for line in refs.splitlines():
        oid, kind = line.decode("ascii").split()
        if not _OID.fullmatch(oid):
            raise GitSourceError("malformed_git_metadata")
        if kind == "commit":
            tips.add(oid)
        elif kind == "tag":
            # Peel tag chains explicitly. Legitimate tags of trees/blobs are
            # not commit history and must not disable the remaining namespace.
            peeled = _git(path, "rev-parse", "--verify", oid + "^{commit}",
                          allowed=(0, 128), limit=256).strip()
            if peeled:
                target = peeled.decode("ascii")
                if not _OID.fullmatch(target):
                    raise GitSourceError("malformed_git_metadata")
                tips.add(target)
    if not tips:
        return b""
    return _git(path, "rev-list", "--reverse", "--topo-order", "--stdin",
                input_data=("\n".join(sorted(tips)) + "\n").encode())


def collect(repo: dict, path: str, author_emails: list[str], eligible, cancelled=None):
    """Collect with cancellation checked before source reads and unit delivery."""
    source = _collect(repo, path, author_emails, eligible)
    while True:
        token = _cancelled.set(cancelled)
        try:
            _check_cancelled()
            try:
                unit = next(source)
            except StopIteration:
                return
            _check_cancelled()
        finally:
            # A generator's suspended frame does not isolate context variables:
            # restore before yielding so interleaved collectors cannot leak state.
            _cancelled.reset(token)
        yield unit


def _collect(repo: dict, path: str, author_emails: list[str], eligible):
    """Yield complete units or content-free exclusion envelopes for terminal marks.

    Eligibility is evaluated before any diff/blob content. Unreachable and future
    commits are not emitted. The caller must honor ``exclusion_reason``.
    """
    authors = {email.strip().casefold() for email in author_emails}
    if not authors:
        return
    if _git(path, "rev-parse", "--is-bare-repository").strip() != b"false":
        raise GitSourceError("bare_repository")
    revisions = _reachable(path)
    local = _local_ignores(path)
    now = time.time()
    for raw_oid in revisions.splitlines():
        _check_cancelled()
        commit = raw_oid.decode("ascii")
        if not _OID.fullmatch(commit):
            raise GitSourceError("malformed_git_metadata")
        # Raw commit objects bypass mailmap, log formatting configuration and
        # signatures. Timestamp is the committer's, identity is the author's.
        unit = _unit(repo, commit, 0.)
        try:
            raw = _git(path, "cat-file", "commit", commit, limit=MAX_SOURCE_BYTES)
            header, separator, message_bytes = raw.partition(b"\n\n")
            if not separator:
                raise GitSourceError("malformed_git_metadata")
            author = re.search(rb"^author .* <([^<>\n]*)> [-0-9]+ [+-][0-9]{4}$", header, re.M)
            committer = re.search(rb"^committer .* <[^<>\n]*> (-?[0-9]+) [+-][0-9]{4}$", header, re.M)
            if not author or not committer:
                raise GitSourceError("invalid_timestamp")
            email = _text(author[1]).strip().casefold()
            timestamp = float(committer[1])
            unit["event_time"] = unit["event_end"] = timestamp
            if email not in authors or timestamp > now or not eligible(timestamp, timestamp):
                continue
            parents = [line[7:].decode("ascii") for line in header.splitlines() if line.startswith(b"parent ")]
            if any(not _OID.fullmatch(parent) for parent in parents):
                raise GitSourceError("malformed_git_metadata")
            budget = [MAX_SOURCE_BYTES - len(raw)]
            blobs, specs = {}, {}
            paths = _changes(path, commit, parents, budget)
            if any(_sensitive(name) for name in paths):
                raise GitSourceError("blocked_path")
            trees = [_tree(path, revision, budget) for revision in [commit, *parents]]
            for name in paths:
                for tree in trees:
                    if _ignored(name, tree, path, budget, blobs, specs, local):
                        raise GitSourceError("ignored_path")
            message = _text(message_bytes).strip()
            items = []
            def add(text, label):
                ref = {"commit": commit}
                if label != "message":
                    ref["path"] = label
                items.append({"id": _digest(unit["id"], label), "text": text, "provenance": "committed",
                              "event_time": timestamp, "source_ref": ref})
            if message:
                add(message, "message")
            patches = []
            for name in paths:
                entries = [tree.get(name) for tree in trees]
                categories = set()
                for entry in entries:
                    if not entry:
                        continue
                    mode, kind, oid = entry
                    if mode == "160000":
                        categories.add("submodule")
                        continue
                    if mode == "120000":
                        categories.add("symlink")
                        continue
                    if kind != "blob":
                        raise GitSourceError("unsupported_git_object")
                    content = _blob(path, oid, budget, blobs)
                    # Inspect entire textual credential JSON, regardless of its
                    # basename, so misleading filenames cannot bypass exclusion.
                    if name.casefold().endswith(".json"):
                        try:
                            account = json.loads(content)
                        except RecursionError:
                            raise GitSourceError("malformed_source_json") from None
                        except (ValueError, UnicodeDecodeError):
                            account = None
                        if isinstance(account, dict) and account.get("type") == "service_account":
                            raise GitSourceError("blocked_path")
                    if b"\0" in content:
                        categories.add("binary")
                    elif content.startswith((b"version https://git-lfs.github.com/spec/v1\n", b"version https://git-lfs.github.com/spec/v1\r\n")):
                        categories.add("lfs")
                    else:
                        try:
                            content.decode("utf-8")
                        except UnicodeDecodeError:
                            categories.add("binary")
                if categories:
                    # For merges only summarize paths in the combined diff.
                    if len(parents) > 1 and not _patch(path, commit, name, budget):
                        continue
                    change = "added" if not any(entries[1:]) else "deleted" if not entries[0] else "changed"
                    summary = f"{name}: {change} ({', '.join(sorted(categories))}; payload omitted)"
                    add(summary, name)
                    # Opaque objects are never evidence; their IDs still keep
                    # different binary changes from collapsing as equivalent.
                    patches.append(summary.encode() + b"\n" + _digest(entries).encode())
                else:
                    patch = _patch(path, commit, name, budget)
                    if patch:
                        patches.append(patch)
                        add(_text(patch), name)
            normalized_message = " ".join(message.split())
            canonical = b"\n".join(patches)
            if len(parents) <= 1 and canonical and not any(b"payload omitted)" in part for part in patches):
                result = _git(path, "patch-id", "--stable", input_data=canonical, limit=1024).split()
                patch_identity = result[0].decode("ascii") if result else hashlib.sha256(canonical).hexdigest()
            else:
                patch_identity = hashlib.sha256(canonical).hexdigest()
            unit.update(paths=paths, items=items, equivalent_id=_digest(repo["id"], email, patch_identity, normalized_message))
            if items:
                yield unit
        except GitSourceError as error:
            if error.code in ("git_timeout", "git_unavailable", "git_read_failed"):
                raise
            unit.update(paths=[], items=[], exclusion_reason=error.code)
            yield unit
