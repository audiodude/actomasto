"""Whole-unit exclusion followed by offline, fail-closed secret redaction."""
from __future__ import annotations

import copy
import importlib.metadata
import math
import re
from collections import Counter
from pathlib import PurePosixPath

POLICY_VERSION = "1:detect-secrets-1.5.0"
MAX_UNIT_BYTES = 32 * 1024 * 1024
SECRET = "[REDACTED_SECRET]"
PATH = "[REDACTED_PATH]"


class PolicyError(Exception):
    """Content-free filtering failure."""


def glob_match(value: str, pattern: str) -> bool:
    """Root-anchored POSIX glob: stars never accidentally cross directories."""
    result = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                index += 1
                if pattern[index + 1:index + 2] == "/":
                    result.append("(?:.*/)?")
                    index += 1
                else:
                    result.append(".*")
            else:
                result.append("[^/]*")
        elif char == "?":
            result.append("[^/]")
        else:
            result.append(re.escape(char))
        index += 1
    return re.fullmatch("".join(result), value) is not None


def sensitive_path(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if name.endswith((".example", ".template")):
        return False
    return (name in {".env", ".dev.vars", ".secrets"}
            or name.startswith((".env.", "credentials"))
            or name.endswith((".pem", ".key"))
            or bool(re.fullmatch(r"(?:service[-_]?account|.*[-_]credentials).*\.json", name)))


# Supplemental rules cover multi-line keys, modern tokens, and assignment syntax
# independently of entropy and the maintained detector's provider-specific rules.
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9 ]* )?PRIVATE KEY-----|\Z)", re.S)
_ASSIGNMENT = re.compile(
    r'''(?ix)(?<![\w])['"]?(?:[\w.-]*[_-])?(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?(?:key|token)|client[_-]?secret|private[_-]?key)['"]?\s*(?:=|:)\s*(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;\}\]]+)'''
)
_CREDENTIAL_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]+@")
_AUTHORIZATION = re.compile(r'''(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*['"]?(?:bearer|basic)\s+(?P<value>[^\s'"]+)''')
_PROVIDER = re.compile(r"\b(?:sk-(?:ant-[A-Za-z0-9_-]+|proj-[A-Za-z0-9_-]+|[A-Za-z0-9_-]{20,})|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b")
_ABSOLUTE_PATH = re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/][^\s<>\"'`]+|~[/\\][^\s<>\"'`]+|/(?!/)[^\s<>\"'`]+)")


class Policy:
    def __init__(self, config: dict):
        self.config = config
        self.rules = config.get("blocklist", {})
        self.last_reason = None
        self._plugins = None
        for rules in [self.rules, *self.rules.get("scoped", [])]:
            for key in ("text", "paths", "repositories"):
                if any(not isinstance(rule, str) or not rule for rule in rules.get(key, [])):
                    raise PolicyError("invalid_blocklist")
        for scope in self.rules.get("scoped", []):
            if not isinstance(scope.get("repository"), str) or not scope["repository"]:
                raise PolicyError("invalid_blocklist")

    def repository_blocked(self, display: str) -> bool:
        return any(glob_match(display, pattern) for pattern in self.rules.get("repositories", []))

    def _applicable(self, display: str):
        yield self.rules
        for scope in self.rules.get("scoped", []):
            if glob_match(display, scope["repository"]):
                yield scope

    def path_blocked(self, path: str, repository_display: str = "") -> bool:
        return any(sensitive_path(part) for part in PurePosixPath(path).parts) or any(
            glob_match(path, pattern)
            for rules in self._applicable(repository_display)
            for pattern in rules.get("paths", [])
        )

    def _text_blocked(self, text: str, display: str) -> bool:
        folded = text.casefold()
        return any(rule.casefold() in folded for rules in self._applicable(display)
                   for rule in rules.get("text", []))

    def _detectors(self):
        if self._plugins is None:
            if importlib.metadata.version("detect-secrets") != "1.5.0":
                raise PolicyError("detector_version")
            from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
            # analyze_string is the pure local matching interface. Unlike
            # analyze_line / scan_line it cannot invoke BasePlugin.verify, even
            # if another library mutates detect-secrets' global settings.
            self._plugins = tuple(cls() for cls in get_mapping_from_secret_type_to_class().values())
            if not self._plugins:
                raise PolicyError("detector_unavailable")
        return self._plugins

    def _redact(self, text: str, remove_paths: bool) -> str:
        try:
            plugins = self._detectors()
            spans = [(m.start(), m.end()) for m in _PRIVATE_KEY.finditer(text)]
            spans.extend((m.start("value"), m.end("value")) for m in _ASSIGNMENT.finditer(text))
            spans.extend((m.start(), m.end() - 1) for m in _CREDENTIAL_URL.finditer(text))
            spans.extend((m.start("value"), m.end("value")) for m in _AUTHORIZATION.finditer(text))
            spans.extend((m.start(), m.end()) for m in _PROVIDER.finditer(text))
            for match in re.finditer(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/=_-])", text):
                value = match.group()
                counts = Counter(value)
                entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
                threshold = 3.0 if re.fullmatch(r"[0-9a-fA-F]+", value) else 4.5
                if entropy > threshold:
                    spans.append((match.start(), match.end()))
            values = set()
            for line in text.splitlines():
                for plugin in plugins:
                    values.update(plugin.analyze_string(line))
            for value in values:
                if not isinstance(value, str) or not value:
                    raise PolicyError("detector_failure")
                spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(value), text))
            merged = []
            markers = [(m.start(), m.end()) for m in re.finditer(r"\[REDACTED_(?:SECRET|PATH)\]", text)]
            for start, end in sorted(spans):
                if any(start >= marker_start and end <= marker_end for marker_start, marker_end in markers):
                    continue
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            output = []
            position = 0
            for start, end in merged:
                output.extend((text[position:start], SECRET))
                position = end
            output.append(text[position:])
            redacted = "".join(output)
            return _ABSOLUTE_PATH.sub(PATH, redacted) if remove_paths else redacted
        except PolicyError:
            raise
        except Exception:
            raise PolicyError("detector_failure") from None

    def filter(self, unit: dict) -> dict | None:
        self.last_reason = None
        display = unit.get("repository_display", unit.get("repository_id", ""))
        reason = None
        if self.repository_blocked(display):
            reason = "blocked_repository"
        elif any(self.path_blocked(path, display) for path in unit["paths"]):
            reason = "blocked_path"
        elif sum(len(item["text"].encode("utf-8")) for item in unit["items"]) > MAX_UNIT_BYTES:
            reason = "oversized_source"
        elif any(self._text_blocked(item["text"], display) for item in unit["items"]):
            reason = "blocked_text"
        # Service-account JSON has no mandated filename. Recognize its schema in
        # original committed content, including diff-prefixed lines.
        elif unit["kind"] == "commit" and any(
            re.search(r'"type"\s*:\s*"service_account"', item["text"])
            for item in unit["items"]
        ):
            reason = "sensitive_credentials"
        if reason:
            self.last_reason = reason
            return None
        try:
            result = copy.deepcopy(unit)
            for item in result["items"]:
                item["text"] = self._redact(item["text"], unit["kind"] == "conversation")
            result["policy_version"] = POLICY_VERSION
            return result
        except PolicyError as exc:
            self.last_reason = str(exc)
            return None

    def output(self, text: str, repository_display: str) -> str:
        if len(text.encode("utf-8")) > MAX_UNIT_BYTES:
            raise PolicyError("oversized_output")
        if self.repository_blocked(repository_display) or self._text_blocked(text, repository_display):
            raise PolicyError("blocked_output")
        redacted = self._redact(text, True)
        # Generated secrets indicate an unsafe response; don't save a rewritten
        # post that no longer corresponds to the model's validated candidate.
        if redacted != text:
            raise PolicyError("unsafe_output")
        return text
