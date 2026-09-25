"""Bounded, read-only personal briefing evidence; independent of draft enrollment."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import sqlite3
import stat
import subprocess
import time
from urllib.parse import quote, urlsplit

MAX_REPOSITORIES = 1024
MAX_DIRECTORIES = 12000
MAX_DEPTH = 12
MAX_COMMITS = 80
MAX_GIT_CALLS = 2400
MAX_COMMAND_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_SCAN_SECONDS = 60
MAX_REMOTE_SECONDS = 40
MAX_REMOTE_HOSTS = 4
MAX_CONVERSATION_SECONDS = 40
MAX_CONVERSATION_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 32 * 1024
MAX_CONTEXT_EXCERPT_BYTES = 4000
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "target",
    ".venv", "venv", "env", "__pycache__", ".cache", ".next", ".nuxt", "coverage",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "Pods",
    "bower_components", "generated", "third_party", "third-party",
})
_GENERATED_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
    "bun.lockb", "uv.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum",
    "composer.lock", "Gemfile.lock", ".DS_Store",
})
_CONTEXT_NAMES = frozenset({"readme.md", "readme.rst", "readme.txt", "readme", "todo.md", "plan.md", "planning.md", "roadmap.md", "next.md"})
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


class BriefingSourceError(RuntimeError):
    """Content-free source failure."""


def _id(*values):
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:24]


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _check(state):
    if time.monotonic() >= state["deadline"] or state["calls"] >= MAX_GIT_CALLS:
        raise BriefingSourceError("source_limit")


def _run(command, *, timeout, limit=MAX_COMMAND_BYTES, payload=None, env=None):
    """Drain both directions without unbounded communicate() allocations."""
    output = bytearray()
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as process:
            try:
                deadline = time.monotonic() + timeout
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(process.stdout.fileno(), False)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    position = 0
                    if payload is not None:
                        os.set_blocking(process.stdin.fileno(), False)
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise BriefingSourceError("source_timeout")
                        for key, _ in selector.select(min(.1, remaining)):
                            if key.fileobj is process.stdin:
                                try:
                                    position += os.write(key.fd, payload[position:position + 65536])
                                except BrokenPipeError:
                                    position = len(payload)
                                if position == len(payload):
                                    selector.unregister(key.fileobj)
                                    process.stdin.close()
                            else:
                                chunk = os.read(key.fd, min(65536, limit + 1 - len(output)))
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                else:
                                    output.extend(chunk)
                                    if len(output) > limit:
                                        raise BriefingSourceError("source_size_limit")
                code = process.wait(timeout=max(.01, deadline - time.monotonic()))
                if code:
                    raise BriefingSourceError("source_unavailable")
            except BaseException:
                process.kill()
                process.wait()
                raise
    except (OSError, subprocess.TimeoutExpired):
        raise BriefingSourceError("source_unavailable") from None
    return bytes(output)


def _git(path, state, *args):
    _check(state)
    state["calls"] += 1
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    env.update(LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1", GIT_PAGER="cat",
               GIT_LITERAL_PATHSPECS="1", GIT_ALLOW_PROTOCOL="")
    command = ["git", "--no-pager", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
               "-c", "protocol.allow=never", "-c", "diff.external=", "-c", "submodule.recurse=false",
               "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "log.showSignature=false",
               "-C", str(path), *args]
    return _run(command, timeout=min(5, max(.01, state["deadline"] - time.monotonic())), env=env)


def _text(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BriefingSourceError("source_encoding") from None


def _meaningful(name):
    parts = PurePosixPath(name).parts
    return bool(parts) and not any(part in _EXCLUDED_DIRS for part in parts) and (
        parts[-1] not in _GENERATED_NAMES and not parts[-1].endswith(
            (".min.js", ".min.css", ".map", ".pyc", ".pyo", ".tsbuildinfo", ".generated.ts", ".generated.js")))


def _safe_path(name):
    return (isinstance(name, str) and bool(name) and not name.startswith("/")
            and all(part not in ("", ".", "..") for part in name.split("/"))
            and not any(ord(char) < 32 or ord(char) == 127 for char in name))


def _origin(raw):
    """Canonical identity only: never resolve remotes, redirects, or Git rewrites."""
    if not raw or raw != raw.strip() or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        return None
    if "://" in raw:
        try:
            value = urlsplit(raw)
            if (value.scheme not in ("https", "ssh") or not value.hostname or value.password
                    or value.query or value.fragment or value.port not in (None, 22 if value.scheme == "ssh" else 443)
                    or value.scheme == "https" and value.username):
                return None
            host, path = value.hostname.lower(), value.path.lstrip("/")
        except ValueError:
            return None
    else:
        match = re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?([A-Za-z0-9.-]+):(.+)", raw)
        if not match:
            return None
        host, path = match[1].lower(), match[2]
    if host == "gitlab-ssh.wikimedia.org":
        host = "gitlab.wikimedia.org"
    path = path.rstrip("/").removesuffix(".git")
    if len(path.split("/")) < 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in (".", "..") for p in path.split("/")):
        return None
    return host + "/" + (path.lower() if host == "github.com" else path)


def _display_blocked(policy, path, display=""):
    return any(policy.repository_blocked(value) for value in (str(path), path.name, display) if value)


def _discover(roots, policy, state, coverage):
    found, visited, pending = [], set(), deque()
    discovery_deadline = time.monotonic() + min(10, max(0, state["deadline"] - time.monotonic()) / 3)
    for configured in roots:
        root = Path(configured)
        if not root.is_absolute():
            raise BriefingSourceError("absolute_root_required")
        pending.append((root.resolve(), 0))
    directories = 0
    while pending:
        if time.monotonic() >= discovery_deadline:
            coverage.append("Repository discovery time capped; discovered projects retained for inspection.")
            break
        _check(state)
        path, depth = pending.popleft()
        if path in visited:
            continue
        visited.add(path)
        if _display_blocked(policy, path):
            continue
        directories += 1
        if directories > MAX_DIRECTORIES or len(found) >= MAX_REPOSITORIES:
            coverage.append("Repository discovery capped; omitted projects are not evidence of inactivity.")
            break
        try:
            with os.scandir(path) as entries:
                children = []
                candidate = False
                for entry in entries:
                    _check(state)
                    if time.monotonic() >= discovery_deadline:
                        coverage.append("Directory enumeration time capped.")
                        break
                    if entry.name == ".git" and not entry.is_symlink():
                        candidate = entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)
                    if (entry.name not in _EXCLUDED_DIRS and entry.is_dir(follow_symlinks=False)
                            and not policy.path_blocked(entry.name, path.name)):
                        children.append(Path(entry.path))
                    if len(children) > MAX_DIRECTORIES:
                        coverage.append("Directory fanout capped.")
                        break
            if candidate:
                found.append(path)
            if depth < MAX_DEPTH:
                available = max(0, MAX_DIRECTORIES - len(pending))
                if len(children) > available:
                    coverage.append("Repository discovery queue capped; deeper coverage incomplete.")
                pending.extend((child, depth + 1) for child in sorted(children)[:available])
            elif children:
                coverage.append("Repository discovery depth capped.")
        except OSError:
            coverage.append("A configured local directory was unavailable.")
    return found


def _evidence(kind, provenance, text, source, *, timestamp=None, identity=None, paths=()):
    return {"id": _id(kind, identity or source), "kind": kind, "provenance": provenance,
            "time": _iso(timestamp) if timestamp is not None else None,
            "text": text, "source": source, "_paths": list(paths)}


def _context(path, display, policy, host):
    evidence = []
    # Only short, explicitly named top-level prose. Never recursively open source,
    # raw conversations, symlinks, unbounded files, or guessed secret locations.
    try:
        names = []
        with os.scandir(path) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_DIRECTORIES:
                    break
                if entry.name.lower() in _CONTEXT_NAMES:
                    names.append(entry.name)
        names.sort()
        for name in names[:3]:
            if policy.path_blocked(name, display):
                continue
            fd = os.open(path / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONTEXT_BYTES:
                    continue
                raw = os.read(fd, MAX_CONTEXT_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > MAX_CONTEXT_BYTES:
                continue
            text = _text(raw)
            evidence.append(_evidence("planning", "observed", "Current repository prose; authorship, acceptance and currency unverified.\n" + text,
                                      f"{host}:{path}/{name}", paths=[name]))
    except (OSError, BriefingSourceError):
        pass
    return evidence


def _repository_identity(path, host, state):
    cached = state["identities"].get(str(path))
    if cached is not None:
        return cached
    # Read raw origin config; never remote get-url or an upstream URL.
    raw = _git(path, state, "config", "--local", "--no-includes", "--null", "--list")
    origins = set()
    for record in raw.split(b"\0"):
        key, _, value = record.partition(b"\n")
        if key == b"remote.origin.url":
            origins.add(_origin(_text(value)))
    origin = next(iter(origins)) if len(origins) == 1 else None
    if origin:
        result = ("git:" + origin, origin, origin.rsplit("/", 1)[-1])
    else:
        common = _text(_git(path, state, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()
        result = ("local:" + _id(host, common), path.name, path.name)
    state["identities"][str(path)] = result
    return result


def _repository(path, host, emails, start, now, policy, state, coverage):
    identity, display, name = _repository_identity(path, host, state)
    if _display_blocked(policy, path, display):
        return None
    try:
        head = _text(_git(path, state, "rev-parse", "--verify", "HEAD")).strip()
    except BriefingSourceError:
        head = None
    if head and not _OID.fullmatch(head):
        raise BriefingSourceError("source_metadata")
    result = {"id": identity, "name": name, "hosts": [host], "last_activity": None,
              "evidence": [], "unfinished": [], "_display": display, "_locations": [[host, str(path)]]}
    if head and emails:
        # Local heads plus current HEAD only: remote-tracking refs, tags and fetched
        # upstream-only branches cannot create personal work.
        author_pattern = "<(" + "|".join(re.escape(email) for email in sorted(emails)) + ")>"
        since = ["--since-as-filter=" + _iso(start)] if start is not None else []
        raw = _git(path, state, "log", "--no-show-signature", "--branches", "HEAD", "--no-merges",
                   "--extended-regexp", "--regexp-ignore-case", "--author=" + author_pattern,
                   *since, "--format=%H%x00%ae%x00%ct%x00", "-z",
                   f"--max-count={MAX_COMMITS + 1}")
        records = [part for part in raw.split(b"\0") if part]
        candidates = []
        for index in range(0, len(records), 3):
            if index + 2 >= len(records):
                raise BriefingSourceError("source_metadata")
            oid, author, stamp = (_text(value).strip() for value in records[index:index + 3])
            if not _OID.fullmatch(oid):
                raise BriefingSourceError("source_metadata")
            try:
                stamp = int(stamp)
            except ValueError:
                raise BriefingSourceError("source_metadata") from None
            if (start is None or start <= stamp) and stamp <= now and author.casefold() in emails:
                candidates.append((stamp, oid))
        if len(records) // 3 > MAX_COMMITS:
            coverage.append("Commit traversal capped in a repository; older or out-of-order history may be incomplete.")
        for stamp, oid in sorted(candidates, reverse=True)[:MAX_COMMITS]:
            cache_key = (identity, oid)
            cached = state["commit_cache"].get(cache_key)
            if cached is None:
                raw_paths = _git(path, state, "diff-tree", "--root", "-r", "--no-commit-id", "--name-only", "-z",
                                 "--no-renames", "--no-ext-diff", "--no-textconv", oid, "--")
                paths = [_text(value) for value in raw_paths.split(b"\0") if value]
                if any(not _safe_path(name) or policy.path_blocked(name, display) for name in paths):
                    continue
                meaningful = [name for name in paths if _meaningful(name)]
                if not meaningful:
                    continue
                # Immutable committed metadata can be reused across clones and
                # worktrees; checkout status and exact source references cannot.
                summary = _text(_git(path, state, "show", "--no-show-signature", "-s", "--format=%B", oid)).strip()
                if len(summary.encode()) > 8000:
                    coverage.append("An oversized commit message was omitted.")
                    continue
                text = summary + "\nChanged source paths: " + ", ".join(meaningful)
                cost = len(text.encode()) + len(raw_paths) + len(identity) + len(oid)
                if state["cache_bytes"] + cost <= MAX_BUNDLE_BYTES:
                    state["commit_cache"][cache_key] = (text, paths)
                    state["cache_bytes"] += cost
            else:
                text, paths = cached
            result["evidence"].append(_evidence("commit", "committed", text, f"{host}:{path}@{oid}",
                                                timestamp=stamp, identity=(identity, oid), paths=paths))
    raw = _git(path, state, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all")
    pieces, index, dirty = raw.split(b"\0"), 0, []
    while index < len(pieces):
        record = pieces[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise BriefingSourceError("source_metadata")
        code, name = _text(record[:2]), _text(record[3:])
        names = [name]
        if "R" in code or "C" in code:
            if index >= len(pieces):
                raise BriefingSourceError("source_metadata")
            names.append(_text(pieces[index]))
            index += 1
        if any(not _safe_path(value.rstrip("/")) or policy.path_blocked(value.rstrip("/"), display) for value in names):
            continue
        if name.endswith("/") or not _meaningful(name):
            continue
        dirty.append(name)
    if dirty:
        text = "Current uncommitted source paths (not dated activity; no recorded next step): " + ", ".join(sorted(set(dirty)))
        result["evidence"].append(_evidence("working_tree", "observed", text, f"{host}:{path} working tree",
                                            paths=dirty))
        result["unfinished"].append(text)
    # Exact checkout references retain per-host/worktree divergence, but are not
    # interpreted as authored work or dated activity.
    if head and result["evidence"]:
        result["evidence"].append(_evidence("working_tree", "observed", "Current checkout HEAD; not proof of authored activity: " + head,
                                            f"{host}:{path} HEAD"))
    return result


def _activity_key(project):
    return max((item["time"] or "" for item in project["evidence"]), default=""), project["id"]


def _retain_metadata(projects):
    """Keep newest useful facts, while retaining lightweight Funes associations."""
    remaining = MAX_BUNDLE_BYTES // 2
    for project in sorted(projects, key=_activity_key, reverse=True):
        retained = []
        for item in sorted(project["evidence"], key=lambda value: value["time"] or "", reverse=True):
            size = len(json.dumps(item).encode())
            if size <= remaining:
                retained.append(item)
                remaining -= size
        project["evidence"] = retained
        project["unfinished"] = [item["text"] for item in retained if item["kind"] == "working_tree"
                                 and item["text"].startswith("Current uncommitted")]


def _scan(config, now, host, policy, seconds=MAX_SCAN_SECONDS):
    coverage, projects = [], []
    state = {"deadline": time.monotonic() + seconds, "calls": 0, "commit_cache": {}, "cache_bytes": 0, "identities": {}}
    emails = {value.strip().casefold() for value in config.get("author_emails", [])}
    if not emails:
        coverage.append("No verified author emails configured; Git commits excluded.")
    start = None if config.get("_project") else now - 30 * 86400
    size = 0
    try:
        paths = _discover(config.get("roots", []), policy, state, coverage)
        if config.get("_project"):
            identities = {}
            selected = set(config.get("_project_ids", []))
            for path in paths:
                _check(state)
                try:
                    identity, display, name = _repository_identity(path, host, state)
                except BriefingSourceError:
                    coverage.append("A repository identity was unavailable during re-entry discovery.")
                    continue
                if _display_blocked(policy, path, display):
                    continue
                identities[path] = identity
                if config["_project"] in (identity, display, name, str(path), path.name):
                    selected.add(identity)
            paths = [path for path, identity in identities.items() if identity in selected]
            coverage.append("Explicit project re-entry includes bounded authored history beyond thirty days; unrelated projects' histories are not read.")
        for path in paths:
            _check(state)
            try:
                item = _repository(path, host, emails, start, now, policy, state, coverage)
            except BriefingSourceError:
                coverage.append("A Git repository was unavailable or exceeded its read bound.")
                continue
            if item is not None:
                projects.append(item)
                size += len(json.dumps(item).encode())
                if size > MAX_BUNDLE_BYTES // 2:
                    _retain_metadata(projects)
                    size = sum(len(json.dumps(value).encode()) for value in projects)
                    coverage.append("Git metadata budget capped; newest authored work prioritized, discovery continues.")
    except BriefingSourceError:
        coverage.append("Git collection time/work limit reached; coverage is incomplete.")
    # Prose cannot consume the activity-discovery budget.
    context_size = 0
    contextualized = 0
    for item in sorted(projects, key=_activity_key, reverse=True):
        explicit = config.get("_project") in (item["id"], item["name"], item["_display"], item["_locations"][0][1])
        if not item["evidence"] and not explicit:
            continue
        if contextualized >= 12:
            coverage.append("Repository prose context limited to twelve work-bearing projects.")
            break
        if time.monotonic() >= state["deadline"]:
            coverage.append("Repository prose context unavailable after collection time bound.")
            break
        contextualized += 1
        for context in _context(Path(item["_locations"][0][1]), item["_display"], policy, host):
            context_size += len(json.dumps(context).encode())
            if context_size <= MAX_BUNDLE_BYTES // 4:
                item["evidence"].append(context)
            else:
                coverage.append("Repository prose context byte budget capped; no activity metadata displaced.")
    coverage.append(f"{host}: inspected {len(projects)} repositories using local objects only; no fetch or activity-absence inference.")
    return {"projects": projects, "coverage": list(dict.fromkeys(coverage))}


def _remote_entry(payload):
    namespace = {"__name__": "briefing_readonly_policy"}
    exec(compile(payload["policy"], "<briefing-policy>", "exec"), namespace)
    payload["config"]["roots"] = [os.path.expanduser(root) for root in payload["config"]["roots"]]
    result = _scan(payload["config"], payload["now"], payload["host"], namespace["Policy"](payload["config"]),
                   seconds=MAX_REMOTE_SECONDS - 5)
    print(json.dumps(result, separators=(",", ":")))


def _remote(remote, config, now):
    from . import policy as policy_module
    host, root = remote.get("name"), remote.get("root")
    if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}", host)
            or not isinstance(root, str) or not root.startswith(("/", "~/")) or "\x00" in root):
        raise BriefingSourceError("invalid_remote")
    # Ship this read-only stdlib worker, not project code or a persisted remote file.
    # Policy pre-read path/repository checks need no detector package on the host;
    # full fail-closed secret filtering happens locally before any returned bundle.
    payload = json.dumps({"code": Path(__file__).read_text(), "policy": Path(policy_module.__file__).read_text(),
                          "config": {"roots": [root], "author_emails": config.get("author_emails", []),
                                     "blocklist": config.get("blocklist", {}), "_project": config.get("_project"),
                                     "_project_ids": config.get("_project_ids", [])},
                          "now": now, "host": host}).encode()
    launcher = "import json,sys; p=json.load(sys.stdin); n={'__name__':'briefing_worker'}; exec(compile(p['code'],'<briefing-worker>','exec'),n); n['_remote_entry'](p)"
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
               "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o", "ClearAllForwardings=yes",
               "-o", "PermitLocalCommand=no", "-T", "--", host, "python3 -c " + shlex.quote(launcher)]
    raw = _run(command, timeout=MAX_REMOTE_SECONDS, limit=MAX_BUNDLE_BYTES + 65536, payload=payload)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BriefingSourceError("invalid_remote_response") from None
    if not isinstance(value, dict) or not isinstance(value.get("projects"), list) or not isinstance(value.get("coverage"), list):
        raise BriefingSourceError("invalid_remote_response")
    try:
        if len(value["projects"]) > MAX_REPOSITORIES or not all(isinstance(note, str) for note in value["coverage"]):
            raise ValueError
        for project in value["projects"]:
            if (not all(isinstance(project[key], str) for key in ("id", "name", "_display"))
                    or project["hosts"] != [host] or not isinstance(project["evidence"], list)
                    or not isinstance(project["unfinished"], list)
                    or not all(isinstance(note, str) for note in project["unfinished"])
                    or not all(location[0] == host and isinstance(location[1], str) and location[1].startswith("/")
                               for location in project["_locations"])):
                raise ValueError
            for item in project["evidence"]:
                if (not all(isinstance(item[key], str) for key in ("id", "kind", "provenance", "text", "source"))
                        or (item["kind"], item["provenance"]) not in
                        {("commit", "committed"), ("working_tree", "observed"), ("planning", "observed")}
                        or not isinstance(item.get("_paths"), list)
                        or not all(isinstance(path, str) for path in item["_paths"])
                        or item["time"] is not None and datetime.fromisoformat(item["time"]).utcoffset() is None):
                    raise ValueError
    except (KeyError, TypeError, ValueError, IndexError):
        raise BriefingSourceError("invalid_remote_response") from None
    return value


def _inventory(config, policy, coverage):
    filename = config.get("inventory_db")
    if not filename:
        coverage.append("Non-Git inventory not configured; non-Git projects may be missing.")
        return []
    roots = [Path(root).resolve() for root in config.get("roots", [])]
    output = []
    try:
        with sqlite3.connect("file:" + quote(str(Path(filename).resolve())) + "?mode=ro", uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = db.execute("SELECT path,name,has_git,scanned_at FROM projects ORDER BY path LIMIT ?", (MAX_REPOSITORIES + 1,))
            for index, (raw, name, has_git, scanned) in enumerate(rows):
                if index == MAX_REPOSITORIES:
                    coverage.append("Inventory project count capped.")
                    break
                path = Path(raw)
                if has_git or not path.is_absolute() or not any(path == root or root in path.parents for root in roots):
                    continue
                if path.resolve() != path or not path.is_dir() or _display_blocked(policy, path):
                    continue
                display = path.name
                text = "Inventory lists this non-Git project; inventory metadata is unverified and may be stale."
                if isinstance(scanned, str):
                    text += " Inventory scanned_at: " + scanned
                output.append({"id": "inventory:" + _id(str(path)), "name": str(name or path.name), "hosts": ["local"],
                               "last_activity": None, "unfinished": [], "_display": display, "_locations": [["local", str(path)]],
                               "evidence": [_evidence("inventory", "inventory", text, "inventory:" + str(path))]
                                           + _context(path, display, policy, "local")})
        coverage.append("Inventory is discovery context only; last_mtime, last_commit and generated next_steps are not activity or accepted intentions.")
    except (OSError, sqlite3.Error, TypeError, ValueError):
        coverage.append("Non-Git inventory unavailable.")
    return output


def _filtered(project, policy):
    display = project["_display"]
    safe = []
    for evidence in project["evidence"]:
        unit = {"kind": evidence["kind"], "repository_display": display, "paths": evidence.get("_paths", []),
                "items": [{"text": evidence["text"]}, {"text": evidence["source"]}]}
        filtered = policy.filter(unit)
        if filtered is None:
            continue
        value = {key: evidence[key] for key in ("id", "kind", "provenance", "time")}
        value.update(text=filtered["items"][0]["text"], source=filtered["items"][1]["text"])
        # Object IDs are validated Git metadata, not secret-bearing source text.
        # Preserve an exact citation if the generic entropy detector masks its SHA.
        oid = evidence["source"].rsplit("@", 1)[-1]
        if evidence["kind"] == "commit" and _OID.fullmatch(oid) and value["source"].endswith("@[REDACTED_SECRET]"):
            value["source"] = value["source"].removesuffix("[REDACTED_SECRET]") + oid
        if value["kind"] == "planning" and len(value["text"].encode()) > MAX_CONTEXT_EXCERPT_BYTES:
            value["text"] = value["text"].encode()[:MAX_CONTEXT_EXCERPT_BYTES].decode("utf-8", errors="ignore") + "\n[Context excerpt; remainder omitted.]"
        safe.append(value)
    labels = policy.filter({"kind": "metadata", "repository_display": display, "paths": [],
                            "items": [{"text": project["name"]}, {"text": project["id"] if project["id"].startswith("git:") else "local_identity"},
                                      *({"text": host} for host in project["hosts"])]})
    if labels is None or project["id"].startswith("git:") and labels["items"][1]["text"] != project["id"]:
        return None
    project.update(name=labels["items"][0]["text"],
                   hosts=[item["text"] for item in labels["items"][2:]], evidence=safe,
                   unfinished=[item["text"] for item in safe if item["kind"] == "working_tree" and item["text"].startswith("Current uncommitted")])
    return project


def _conversations(config, projects, policy, now, coverage):
    from .funes_source import FunesSource, SourceError
    harnesses = config.get("conversation_harnesses", [])
    if not harnesses or not config.get("funes"):
        coverage.append("Conversations unavailable: no separate briefing conversation enrollment; no raw private transcripts or pending collector units read.")
        return
    cursors, size, count = {}, 0, 0
    def check():
        if time.monotonic() >= deadline or count >= 300 or size >= MAX_CONVERSATION_BYTES:
            raise BriefingSourceError("conversation_limit")
    repositories = [{"id": item["id"], "paths": [path for host, path in item["_locations"] if host == "local"]}
                    for item in projects]
    by_id = {item["id"]: item for item in projects}
    def eligible(identity, start, end):
        return (identity in by_id and start <= end <= now
                and (bool(config.get("_project")) or now - 30 * 86400 <= start))
    try:
        source = FunesSource(config["funes"], cancelled=check)
        for harness in harnesses:
            deadline = time.monotonic() + MAX_CONVERSATION_SECONDS
            staged = []
            try:
                for unit in source.collect(harness, repositories, cursors.get, cursors.__setitem__, eligible):
                    check()
                    count += 1
                    project = by_id[unit["repository_id"]]
                    unit["repository_display"] = project["_display"]
                    filtered = policy.filter(unit)
                    if filtered is None:
                        continue
                    unit_size = sum(len(item["text"].encode()) for item in filtered["items"])
                    if size + unit_size > MAX_CONVERSATION_BYTES:
                        raise BriefingSourceError("conversation_limit")
                    size += unit_size
                    # Preserve complete filtered messages; do not clip text before
                    # policy checks or quietly truncate a user's qualification.
                    for item in filtered["items"]:
                        ref = item.get("source_ref", unit["source_ref"])
                        source_ref = (f"{ref['client']} session {ref['session_id']} message " + ",".join(ref["message_ids"]))
                        safe_ref = policy.filter({"kind": "metadata", "repository_display": project["_display"],
                                                  "paths": [], "items": [{"text": source_ref}]})
                        masked_ids = re.sub(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b", "[REDACTED_SECRET]", source_ref)
                        if safe_ref is None or safe_ref["items"][0]["text"] not in (source_ref, masked_ids):
                            raise BriefingSourceError("unsafe_source_reference")
                        value = _evidence("conversation", item["provenance"], item["text"], source_ref,
                                          timestamp=item.get("event_time", unit["event_time"]), identity=item["id"])
                        value.pop("_paths")
                        staged.append((project, value))
                if source.status.get("coverage") != "current":
                    coverage.append(f"{harness} conversations unavailable: stale source coverage.")
                    continue
                if (source.status.get("pending_turns", 0)
                        or any(status != "complete" and amount for status, amount in source.status.get("streams", {}).items())):
                    coverage.append(f"{harness}: partial coverage; only individually complete turns retained, pending/incomplete turns excluded.")
                for project, value in staged:
                    project["evidence"].append(value)
                coverage.append(f"{harness}: complete Funes turns read with separate briefing consent; assistant statements remain historical reports, not verified outcomes.")
            except BriefingSourceError as error:
                if str(error) == "conversation_limit":
                    for project, value in staged:
                        project["evidence"].append(value)
                    coverage.append(f"{harness}: conversation time/size limit; validated complete turns retained with incomplete coverage.")
                else:
                    coverage.append(f"{harness} conversations unavailable: unsafe source reference.")
            except SourceError:
                coverage.append(f"{harness} conversations unavailable: incompatible/stale source or revision; staged results excluded.")
    except SourceError:
        coverage.append("Conversations unavailable: incompatible read-only Funes configuration.")


def collect(config: dict, *, now: datetime, project: str | None = None) -> dict:
    """Collect filtered facts, current observations and explicitly enrolled turns."""
    from .policy import Policy, PolicyError
    if now.tzinfo is None or now.utcoffset() is None:
        raise BriefingSourceError("aware_time_required")
    config = {**config, "_project": project}
    try:
        policy = Policy(config)
    except (PolicyError, TypeError, AttributeError):
        raise BriefingSourceError("invalid_policy") from None
    stamp = now.timestamp()
    bundle = _scan(config, stamp, "local", policy)
    raw_projects = bundle["projects"]
    coverage = bundle["coverage"]
    if project:
        config["_project_ids"] = list({item["id"] for item in raw_projects})
    remotes = config.get("remote_hosts", [])
    for remote in remotes[:MAX_REMOTE_HOSTS]:
        try:
            remote_bundle = _remote(remote, config, stamp)
            raw_projects.extend(remote_bundle["projects"])
            coverage.extend(remote_bundle["coverage"])
            if project:
                config["_project_ids"] = list({item["id"] for item in raw_projects})
        except BriefingSourceError:
            host = remote.get("name", "remote")
            if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,253}", host):
                host = "remote"
            coverage.append(f"{host}: unavailable (read-only SSH failed or timed out); no inference of inactivity.")
    if len(remotes) > MAX_REMOTE_HOSTS:
        coverage.append("Remote host count capped.")
    raw_projects.extend(_inventory(config, policy, coverage))
    selected = None
    if project:
        selected = {item["id"] for item in raw_projects
                    if project in (item["id"], item["name"], item["_display"])
                    or any(project in (path, Path(path).name) for _, path in item["_locations"])}
        if len(selected) > 1:
            raise BriefingSourceError("ambiguous_project")
    merged = {}
    for item in raw_projects:
        if selected is not None and item["id"] not in selected:
            continue
        item = _filtered(item, policy)
        if item is None:
            continue
        existing = merged.get(item["id"])
        if existing:
            existing["hosts"] = sorted(set(existing["hosts"] + item["hosts"]))
            existing["evidence"].extend(item["evidence"])
            existing["unfinished"].extend(item["unfinished"])
            existing["_locations"].extend(item["_locations"])
        else:
            merged[item["id"]] = item
    projects = list(merged.values())
    _conversations(config, projects, policy, stamp, coverage)
    size, output = 0, []
    for item in sorted(projects, key=_activity_key, reverse=True):
        if not project and not any(e["kind"] in ("commit", "conversation", "inventory")
                                   or e["kind"] == "working_tree" and e["text"].startswith("Current uncommitted")
                                   for e in item["evidence"]):
            continue
        unique = {}
        for evidence in item["evidence"]:
            previous = unique.get(evidence["id"])
            if previous is not None:
                sources = previous["source"].split("\n")
                if evidence["source"] not in sources:
                    previous["source"] += "\n" + evidence["source"]
            else:
                unique[evidence["id"]] = evidence
        item["evidence"] = sorted(unique.values(), key=lambda evidence: (evidence["time"] or "", evidence["id"]), reverse=True)
        dates = [evidence["time"] for evidence in item["evidence"] if evidence["kind"] in ("commit", "conversation") and evidence["time"]]
        item["last_activity"] = max(dates, default=None)
        item["unfinished"] = list(dict.fromkeys(item["unfinished"]))
        item.pop("_display")
        item.pop("_locations")
        size += len(json.dumps(item).encode())
        if size > MAX_BUNDLE_BYTES:
            coverage.append("Combined evidence output capped; some projects omitted.")
            break
        output.append(item)
    coverage.append("Current working trees and repository prose are undated context, not proof of yesterday's activity or accepted intentions. Missing evidence does not establish inactivity.")
    coverage.append("Retained Actomasto draft excerpts are not complete conversation evidence and are not read; collector enrollment and publishing are unchanged.")
    safe_coverage = []
    for note in dict.fromkeys(coverage):
        filtered = policy.filter({"kind": "metadata", "repository_display": "", "paths": [], "items": [{"text": note}]})
        safe_coverage.append(filtered["items"][0]["text"] if filtered else "Some source coverage information was excluded by policy.")
    return {"projects": sorted(output, key=lambda item: (item["last_activity"] or "", item["id"]), reverse=True),
            "coverage": list(dict.fromkeys(safe_coverage)), "blocklist": config.get("blocklist", {})}
