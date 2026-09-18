"""Origin-only enrollment and anonymous, explicit public-visibility checks."""
from __future__ import annotations

from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import stat
import re
import time
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import git_source
from .git_source import GitSourceError, _git

HOSTS = frozenset(("github.com", "gitlab.com", "gitlab.wikimedia.org"))
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_DISCOVERY_SECONDS = 60
MAX_DISCOVERY_DEPTH = 128
MAX_DISCOVERY_RESULTS = 10_000


def normalize_origin(url):
    if not isinstance(url, str) or not url or url != url.strip() or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("invalid_origin")
    if "://" not in url:
        match = re.fullmatch(r"(?:([A-Za-z0-9_.-]+)@)?([A-Za-z0-9.-]+):(.+)", url)
        if not match:
            raise ValueError("invalid_origin")
        host, project = match[2].lower(), match[3]
        ssh = True
    else:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ValueError("invalid_origin") from None
        if parsed.scheme not in ("https", "ssh") or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("invalid_origin")
        ssh = parsed.scheme == "ssh"
        if parsed.password is not None or (not ssh and parsed.username is not None):
            raise ValueError("credential_origin")
        if ssh and parsed.username is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+", parsed.username):
            raise ValueError("credential_origin")
        if port not in (None, 22 if ssh else 443):
            raise ValueError("unsupported_origin_port")
        host, project = parsed.hostname.lower(), parsed.path.lstrip("/")
    if ssh and host == "gitlab-ssh.wikimedia.org":
        host = "gitlab.wikimedia.org"
    if host not in HOSTS:
        raise ValueError("unsupported_host")
    # Reject encoded separators, controls and credentials rather than interpreting
    # ambiguous URL spellings differently from the host's API.
    if unquote(project) != project or "\\" in project:
        raise ValueError("invalid_origin")
    project = project.rstrip("/")
    if project.endswith(".git"):
        project = project[:-4]
    components = project.split("/")
    if len(components) < 2 or (host == "github.com" and len(components) != 2):
        raise ValueError("invalid_origin")
    if any(part in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in components):
        raise ValueError("invalid_origin")
    if host == "github.com":
        project = project.lower()
    return host + "/" + project


def _candidate(path):
    result = {"path": str(path), "origin": None, "host": None, "project_path": None, "reason": None}
    try:
        if _git(path, "rev-parse", "--is-bare-repository").strip() == b"true":
            result["reason"] = "bare_repository"
            return result
        # Raw local fetch URLs, not `remote get-url` (which applies insteadOf
        # rewrites), and never upstream/push URLs or arbitrary remote helpers.
        raw = _git(path, "config", "--local", "--no-includes", "-z", "--get-all", "remote.origin.url", allowed=(0, 1))
        urls = [value.decode("utf-8") for value in raw.split(b"\0") if value]
        if not urls:
            result["reason"] = "missing_origin"
            return result
        origins = {normalize_origin(url) for url in urls}
        if len(origins) != 1:
            result["reason"] = "conflicting_origin"
            return result
        origin = origins.pop()
        host, project = origin.split("/", 1)
        result.update(origin=origin, host=host, project_path=project)
    except ValueError as error:
        result["reason"] = str(error) if str(error) in {"invalid_origin", "credential_origin", "unsupported_host", "unsupported_origin_port"} else "invalid_origin"
    except GitSourceError as error:
        result["reason"] = error.code
    return result


def discover(roots, cancelled=None):
    """Enumerate exhaustively with bounded time, open handles, and result memory."""
    results, seen = [], {}
    deadline = time.monotonic() + MAX_DISCOVERY_SECONDS
    stack = []

    def check():
        if time.monotonic() >= deadline:
            raise GitSourceError("discovery_timeout")
        if cancelled is not None:
            cancelled()

    def append(result):
        if len(results) >= MAX_DISCOVERY_RESULTS:
            raise GitSourceError("discovery_limit")
        results.append(result)

    def metadata(fd, name):
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def enter(path, name, parent_fd=None):
        check()
        if len(stack) >= MAX_DISCOVERY_DEPTH:
            raise GitSourceError("discovery_limit")
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            dotgit = metadata(fd, ".git")
            candidate = dotgit is not None and (stat.S_ISDIR(dotgit.st_mode) or stat.S_ISREG(dotgit.st_mode))
            bare = not candidate and all(metadata(fd, name) is not None for name in ("HEAD", "objects", "refs"))
            if candidate or bare:
                # Traversal is anchored to no-follow directory descriptors. Before
                # invoking Git by pathname, also reject a renamed/replaced path.
                current = path.stat(follow_symlinks=False)
                opened = os.fstat(fd)
                if path.resolve() != path or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise GitSourceError("discovery_changed")
                result = _candidate(path)
                check()
                if path not in seen:
                    append(result)
                    seen[path] = result
                if result["reason"] == "bare_repository":
                    return
            stack.append((path, fd, os.scandir(fd)))
            fd = None
        finally:
            if fd is not None:
                os.close(fd)

    def unreadable(path):
        result = {"path": str(path), "origin": None, "host": None, "project_path": None, "reason": "discovery_unreadable"}
        if path in seen:
            seen[path].update(result)
        else:
            append(result)
            seen[path] = result

    # Candidate Git subprocesses use the same deadline and cancellation boundary.
    token = git_source._cancelled.set(check)
    try:
        for configured in roots:
            check()
            root_input = Path(configured)
            if not root_input.is_absolute():
                raise ValueError("absolute_discovery_root_required")
            # Only an explicitly configured root may resolve through a symlink.
            root = root_input.resolve()
            try:
                enter(root, root)
            except OSError:
                unreadable(root)
            while stack:
                check()
                path, fd, entries = stack[-1]
                try:
                    entry = next(entries)
                except (StopIteration, OSError) as error:
                    if isinstance(error, OSError):
                        unreadable(path)
                    entries.close()
                    os.close(fd)
                    stack.pop()
                    continue
                try:
                    if entry.name != ".git" and entry.is_dir(follow_symlinks=False):
                        enter(path / entry.name, entry.name, fd)
                except OSError:
                    unreadable(path / entry.name)
        check()
        return sorted(results, key=lambda result: result["path"])
    finally:
        for _, fd, entries in stack:
            entries.close()
            os.close(fd)
        git_source._cancelled.reset(token)


def _retry_after(headers):
    value = headers.get("retry-after", "")
    try:
        seconds = float(value)
        if seconds >= 0 and seconds < float("inf"):
            return max(60., seconds)
    except (ValueError, TypeError):
        pass
    try:
        return max(60., parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 60.


def verify(origin, http_client=None):
    """A stateless check; the daemon owns cache lifetimes and backoff attempts."""
    result = {"state": "uncertain", "id": None, "retry_after": 60., "reason": "visibility_uncertain"}
    try:
        normalized = normalize_origin("https://" + origin)
        if normalized != origin:
            raise ValueError("invalid_origin")
    except (ValueError, TypeError):
        result["reason"] = "invalid_origin"
        return result
    host, project = normalized.split("/", 1)
    url = ("https://api.github.com/repos/" + project if host == "github.com"
           else "https://" + host + "/api/v4/projects/" + quote(project, safe=""))
    own_client = http_client is None
    client = http_client or httpx.Client(follow_redirects=False, timeout=15., trust_env=False)
    try:
        # Do not decode compressed server input: a compression bomb could exceed
        # the inspection cap before an iterator yielded its first decoded chunk.
        with client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "actomasto/1"},
                           follow_redirects=False, timeout=15., auth=None) as response:
            result["retry_after"] = _retry_after(response.headers)
            if response.status_code != 200:
                result["reason"] = "visibility_http_" + str(response.status_code)
                return result
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                result["reason"] = "visibility_malformed"
                return result
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    result["reason"] = "visibility_oversized"
                    return result
            try:
                body = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                result["reason"] = "visibility_malformed"
                return result
            if not isinstance(body, dict) or type(body.get("id")) is not int or body["id"] <= 0:
                result["reason"] = "visibility_malformed"
                return result
            if host == "github.com":
                visibility = body.get("private")
                if type(visibility) is not bool:
                    result["reason"] = "visibility_malformed"
                    return result
                public = not visibility
            else:
                visibility = body.get("visibility")
                if visibility not in ("public", "private", "internal"):
                    result["reason"] = "visibility_malformed"
                    return result
                public = visibility == "public"
            result.update(state="public" if public else "private", id=f"{host}:{body['id']}",
                          reason="verified_public" if public else "verified_private", retry_after=0.)
            return result
    except (httpx.HTTPError, OSError, ValueError):
        result["reason"] = "visibility_network"
        return result
    finally:
        if own_client:
            client.close()
