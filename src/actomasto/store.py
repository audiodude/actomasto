"""Authoritative, private SQLite lifecycle and recoverable suggestion export."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid

DAY = 86400
MODEL = "claude-haiku-4-5-20251001"


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class Store:
    def __init__(self, data_dir: Path):
        self.lock = RLock()
        self.dispatch_lock = RLock()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.path = self.data_dir / "state.sqlite3"
        self.export_path = self.data_dir / "suggestions.jsonl"
        os.umask(0o077)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise ValueError("unsupported_database_version")
        if version == 0:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE intervals (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
                    start REAL NOT NULL, end REAL, CHECK(end IS NULL OR end>=start));
                CREATE UNIQUE INDEX open_interval ON intervals(scope,kind) WHERE end IS NULL;
                CREATE TABLE repositories (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE source_cursors (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE source_markers (repository_id TEXT NOT NULL, id TEXT NOT NULL,
                    equivalent_id TEXT, reason TEXT NOT NULL, event_time REAL, PRIMARY KEY(repository_id,id));
                CREATE INDEX equivalent_marker ON source_markers(repository_id,equivalent_id);
                CREATE TABLE pending_units (id TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
                    equivalent_id TEXT, payload TEXT NOT NULL, bytes INTEGER NOT NULL,
                    collected_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0, malformed_count INTEGER NOT NULL DEFAULT 0,
                    ready_at REAL NOT NULL, adapter TEXT NOT NULL, policy_revision INTEGER NOT NULL);
                CREATE INDEX queue_expiry ON pending_units(expires_at);
                CREATE TABLE adapters (adapter TEXT PRIMARY KEY, healthy INTEGER NOT NULL);
                CREATE TABLE periods (id TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
                    timezone TEXT NOT NULL, spent INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE attempts (id TEXT PRIMARY KEY, unit_ids TEXT NOT NULL, repository_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL, period TEXT NOT NULL REFERENCES periods(id), model TEXT NOT NULL,
                    input_rate INTEGER NOT NULL, output_rate INTEGER NOT NULL, reservation INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, dispatched_at REAL NOT NULL,
                    state TEXT NOT NULL, usage TEXT, charge INTEGER, estimated INTEGER,
                    policy_revision INTEGER NOT NULL, character_limit INTEGER NOT NULL);
                CREATE TABLE suggestions (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    repository_id TEXT NOT NULL, created_at REAL NOT NULL, record TEXT NOT NULL);
                CREATE TABLE evidence (id TEXT PRIMARY KEY, suggestion_id TEXT NOT NULL REFERENCES suggestions(id)
                    ON DELETE CASCADE, record TEXT NOT NULL);
                CREATE TABLE export_outbox (suggestion_id TEXT PRIMARY KEY REFERENCES suggestions(id)
                    ON DELETE CASCADE, sequence INTEGER UNIQUE NOT NULL, exported INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE events (id INTEGER PRIMARY KEY, code TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE event_states (code TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE counters (code TEXT PRIMARY KEY, count INTEGER NOT NULL);
                PRAGMA user_version=1;
            """)
            self.db.execute("INSERT INTO settings VALUES(1,?)", (_json({
                "enabled": False, "ever_enabled": False, "epoch": 0, "config": {},
                "config_revision": 0, "period": None, "budget_paused": False,
                "generation_paused": False, "generation_pause_revision": None,
                "generation_pause_code": None, "last_clock": None, "clock_uncertain": False,
                "export_rebuild": True,
            }),))
            self.db.execute("COMMIT")
        self._permissions()

    def _permissions(self):
        for name in ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm", "suggestions.jsonl"):
            path = self.data_dir / name
            if path.exists():
                path.chmod(0o600)

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _settings(self):
        return json.loads(self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    def _save_settings(self, settings):
        self.db.execute("UPDATE settings SET value=? WHERE id=1", (_json(settings),))

    @_locked
    def settings(self):
        return self._settings()

    def _counter(self, code):
        self.db.execute("INSERT INTO counters VALUES(?,1) ON CONFLICT(code) DO UPDATE SET count=count+1", (code,))

    def _event(self, code, state, now, *, count=True):
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", code) or state not in ("error", "ok", "warning", "paused", "recovered"):
            raise ValueError("invalid_event_code")
        old = self.db.execute("SELECT state FROM event_states WHERE code=?", (code,)).fetchone()
        if not old or old[0] != state:
            self.db.execute("INSERT INTO event_states VALUES(?,?) ON CONFLICT(code) DO UPDATE SET state=excluded.state", (code, state))
            self.db.execute("INSERT INTO events(code,state,created_at) VALUES(?,?,?)", (code, state, now))
            if count:
                self._counter(code)
        self.db.execute("DELETE FROM events WHERE created_at<?", (now - 30 * DAY,))

    @_locked
    def event(self, code, state="error", now=None):
        with self._transaction():
            self._event(code, state, time.time() if now is None else now)

    def _open_interval(self, scope, kind, now):
        self.db.execute("INSERT INTO intervals(scope,kind,start) SELECT ?,?,? WHERE NOT EXISTS "
                        "(SELECT 1 FROM intervals WHERE scope=? AND kind=? AND end IS NULL)",
                        (scope, kind, now, scope, kind))

    def _close_interval(self, scope, kind, now):
        self.db.execute("UPDATE intervals SET end=max(start,?) WHERE scope=? AND kind=? AND end IS NULL", (now, scope, kind))

    def _clock(self, now):
        settings = self._settings()
        last = settings["last_clock"]
        uncertain = last is not None and now < last
        settings["clock_uncertain"] = uncertain
        if not uncertain:
            settings["last_clock"] = now
        self._save_settings(settings)
        if uncertain:
            self._event("clock_uncertain", "error", now)
        return not uncertain

    def _period(self, now):
        settings = self._settings()
        current = self.db.execute("SELECT * FROM periods WHERE id=?", (settings["period"],)).fetchone()
        if current and now < current["end"]:
            return dict(current)
        zone_name = settings["config"].get("budget", {}).get("timezone", "UTC")
        zone = ZoneInfo(zone_name)
        local = datetime.fromtimestamp(now, zone)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        # On timezone changes never overlap or reopen the preceding accounting period.
        begin = max(start.timestamp(), current["end"] if current else start.timestamp())
        period_id = f"{int(begin)}:{int(end.timestamp())}:{zone_name}"
        self.db.execute("INSERT OR IGNORE INTO periods(id,start,end,timezone) VALUES(?,?,?,?)", (period_id, begin, end.timestamp(), zone_name))
        settings["period"] = period_id
        if settings["budget_paused"]:
            self._close_interval("global", "budget", current["end"] if current else now)
            settings["budget_paused"] = False
            self._event("budget_paused", "recovered", now)
        self._save_settings(settings)
        return dict(self.db.execute("SELECT * FROM periods WHERE id=?", (period_id,)).fetchone())

    def _cap(self, settings=None):
        settings = settings or self._settings()
        return int(Decimal(str(settings["config"].get("budget", {}).get("monthly_usd", "20.00"))) * 1_000_000)

    @_locked
    def apply_config(self, config, now):
        with self._transaction():
            self._clock(now)
            settings = self._settings()
            old_cap = self._cap(settings)
            old_config = settings["config"]
            settings.update(config=config, config_revision=settings["config_revision"] + 1,
                            epoch=settings["epoch"] + 1, generation_paused=False,
                            generation_pause_revision=None, generation_pause_code=None)
            self._save_settings(settings)
            period = self._period(now)
            settings = self._settings()
            if settings["budget_paused"] and self._cap(settings) > old_cap and period["spent"] + period["reserved"] < self._cap(settings):
                settings["budget_paused"] = False
                self._close_interval("global", "budget", now)
                self._save_settings(settings)
            from .policy import Policy
            policy = Policy(config)
            old_authors = {email.strip().casefold() for email in old_config.get("identity", {}).get("author_emails", [])}
            new_authors = {email.strip().casefold() for email in config.get("identity", {}).get("author_emails", [])}
            invalidated_adapters = {"git"} if old_authors - new_authors else set()
            old_sources, new_sources = old_config.get("sources", {}), config.get("sources", {})
            invalidated_adapters.update(client for client in ("claude", "codex", "omp")
                                        if old_sources.get(client + "_root") != new_sources.get(client + "_root"))
            for repo in self.repositories():
                paths = [p for p in repo["paths"] if self._in_roots(p, config)]
                if not paths or policy.repository_blocked(repo["display_path"]):
                    self._revoke(repo, now, "revoked")
                else:
                    if paths != repo["paths"]:
                        self._revoke(repo, now, "revoked")
                    repo["paths"] = paths
                    self._save_repo(repo)
            for row in self.db.execute("SELECT * FROM pending_units").fetchall():
                unit = json.loads(row["payload"])
                # Filtered units intentionally carry no author or private source
                # path, so narrowed authorization cannot be rechecked in place.
                if row["adapter"] in invalidated_adapters:
                    self._mark(unit, "revoked")
                    continue
                repo = json.loads(self.db.execute("SELECT data FROM repositories WHERE id=?", (unit["repository_id"],)).fetchone()[0])
                unit["repository_display"] = repo["display_path"]
                # Original secret-bearing text is deliberately unavailable. A new
                # literal exclusion cannot safely be checked against redaction.
                old_policy = Policy(old_config)
                old_text = {text.casefold() for rules in old_policy._applicable(repo["display_path"])
                            for text in rules.get("text", [])}
                new_text = {text.casefold() for rules in policy._applicable(repo["display_path"])
                            for text in rules.get("text", [])}
                if new_text - old_text:
                    self._mark(unit, "blocked")
                    continue
                checked = policy.filter(unit)
                if checked is None:
                    self._mark(unit, "blocked")
                else:
                    payload = _json(checked)
                    self.db.execute("UPDATE pending_units SET payload=?,bytes=?,policy_revision=? WHERE id=?",
                                    (payload, len(payload.encode()), settings["config_revision"], row["id"]))

    @_locked
    def set_enabled(self, enabled, now):
        with self._transaction():
            self._clock(now)
            self._period(now)
            settings = self._settings()
            if settings["enabled"] == bool(enabled):
                return
            settings["enabled"] = bool(enabled)
            settings["epoch"] += 1
            boundary = max(now, settings["last_clock"] or now)
            if enabled:
                settings["ever_enabled"] = True
                self._close_interval("global", "off", boundary)
            elif settings["ever_enabled"]:
                self._open_interval("global", "off", boundary)
            self._save_settings(settings)
            self._expire(now)

    @staticmethod
    def _in_roots(path, config):
        candidate = Path(path)
        return any(candidate == Path(root) or Path(root) in candidate.parents for root in config.get("discovery", {}).get("roots", []))

    def _save_repo(self, repo):
        self.db.execute("INSERT INTO repositories VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (repo["id"], _json(repo)))

    def _revoke(self, repo, now, state):
        if repo["state"] not in ("revoked", "private"):
            self._open_interval(repo["id"], "revoked", now)
            settings = self._settings()
            settings["epoch"] += 1
            self._save_settings(settings)
        repo["state"] = state
        repo["reason"] = state
        repo["public_until"] = 0
        self._save_repo(repo)
        for row in self.db.execute("SELECT payload FROM pending_units WHERE repository_id=?", (repo["id"],)).fetchall():
            self._mark(json.loads(row[0]), "revoked")

    @_locked
    def repositories(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM repositories ORDER BY id")]

    @_locked
    def sync_repositories(self, discovered, verified, now):
        with self._transaction():
            self._clock(now)
            self._period(now)
            settings = self._settings()
            if not settings["enabled"] or settings["budget_paused"] or settings["clock_uncertain"]:
                return self.repositories()
            config = settings["config"]
            from .policy import Policy
            policy = Policy(config)
            discovered_by_path = {entry["path"]: entry for entry in discovered}
            old = {r["id"]: r for r in self.repositories()}
            by_origin = {r["origin"]: r for r in old.values()}
            groups = {}
            for entry in discovered:
                origin = entry.get("origin")
                if not origin or entry.get("reason") or not self._in_roots(entry["path"], config):
                    continue
                if policy.repository_blocked(origin):
                    continue
                result = verified.get(origin, {"state": "uncertain"})
                prior = by_origin.get(origin)
                identity = result.get("id") if result.get("state") == "public" else (prior or {}).get("id")
                if not identity:
                    continue
                groups.setdefault(identity, []).append((entry, result))
            for identity, repo in old.items():
                changed_clone = any(
                    path in discovered_by_path
                    and discovered_by_path[path].get("origin") != repo["origin"]
                    for path in repo["paths"]
                )
                if changed_clone:
                    self._revoke(repo, now, "revoked")
                if identity not in groups:
                    self._revoke(repo, now, "revoked")
            for identity, entries in groups.items():
                entry, result = entries[0]
                origin = entry["origin"]
                checked_at = result.get("checked_at", now)
                expires_at = result.get("expires_at", now + DAY)
                cache_valid = (isinstance(checked_at, (int, float)) and isinstance(expires_at, (int, float))
                               and math.isfinite(checked_at) and math.isfinite(expires_at))
                if cache_valid:
                    checked_at = min(now, checked_at)
                    expires_at = min(expires_at, checked_at + DAY)
                else:
                    checked_at, expires_at = now, 0
                repo = old.get(identity)
                if repo and repo["origin"] != origin:
                    self._revoke(repo, now, "revoked")
                if repo is None:
                    if result["state"] != "public" or expires_at <= now:
                        continue
                    repo = {"id": identity, "import_start": now - 7 * DAY, "import_end": now,
                            "import_complete": False, "state": "new", "public_until": 0}
                repo.update(paths=sorted({e["path"] for e, _ in entries}), origin=origin,
                            display_path=origin, host=entry.get("host") or origin.split("/", 1)[0],
                            project_path=entry.get("project_path") or origin.split("/", 1)[1])
                if any(r.get("state") == "private" for _, r in entries):
                    self._revoke(repo, now, "private")
                    continue
                if result["state"] == "public" and expires_at > now:
                    if repo["state"] in ("revoked", "private"):
                        self._close_interval(identity, "revoked", now)
                    repo.update(state="public", public_until=expires_at, public_checked_at=checked_at, reason=None)
                elif repo["public_until"] <= now and repo["state"] not in ("revoked", "private"):
                    repo.update(state="uncertain", reason="visibility_uncertain")
                self._save_repo(repo)
            return self.repositories()

    @_locked
    def eligible(self, repository_id, start, end):
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or end < start:
            return False
        row = self.db.execute("SELECT data FROM repositories WHERE id=?", (repository_id,)).fetchone()
        if not row:
            return False
        repo = json.loads(row[0])
        settings = self._settings()
        if repo["state"] in ("revoked", "private") or start < repo["import_start"]:
            return False
        if settings["last_clock"] is not None and end > settings["last_clock"]:
            return False
        # Intervals are half-open; a zero-duration event is a point membership test.
        comparison = "start<=?" if start == end else "start<?"
        return self.db.execute("SELECT 1 FROM intervals WHERE scope IN ('global',?) "
            f"AND {comparison} AND (end IS NULL OR end>?) LIMIT 1", (repository_id, end, start)).fetchone() is None

    def _seen(self, unit):
        repo = unit["repository_id"]
        equivalent = unit.get("equivalent_id")
        for table in ("source_markers", "pending_units"):
            if self.db.execute(f"SELECT 1 FROM {table} WHERE repository_id=? AND (id=? OR (? IS NOT NULL AND equivalent_id=?)) LIMIT 1",
                               (repo, unit["id"], equivalent, equivalent)).fetchone():
                return True
        return False

    @_locked
    def seen(self, unit):
        return self._seen(unit)

    def _mark(self, unit, reason):
        if not re.fullmatch(r"[a-z_]{1,60}", reason):
            raise ValueError("invalid_marker_reason")
        marker = self.db.execute("INSERT OR IGNORE INTO source_markers VALUES(?,?,?,?,?)",
                        (unit["repository_id"], unit["id"], unit.get("equivalent_id"), reason, unit.get("event_time")))
        self.db.execute("DELETE FROM pending_units WHERE id=? AND repository_id=?", (unit["id"], unit["repository_id"]))
        if marker.rowcount:
            self._counter(reason)

    @_locked
    def mark(self, unit, reason):
        with self._transaction():
            self._mark(unit, reason)

    def _expire(self, now):
        rows = self.db.execute("SELECT payload FROM pending_units WHERE expires_at<=?", (now,)).fetchall()
        for row in rows:
            self._mark(json.loads(row[0]), "expired")
        return len(rows)

    @_locked
    def expire(self, now):
        with self._transaction():
            self._clock(now)
            self._period(now)
            count = self._expire(now)
        if count:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count

    def _insert_unit(self, unit, collected, expires, attempts=0, malformed=0, ready=None):
        payload = _json(unit)
        self.db.execute("INSERT INTO pending_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (unit["id"], unit["repository_id"], unit.get("equivalent_id"), payload, len(payload.encode()),
             collected, expires, "pending", attempts, malformed, collected if ready is None else ready,
             unit.get("adapter", "git"), self._settings()["config_revision"]))

    @_locked
    def enqueue(self, filtered_unit, now):
        with self._transaction():
            self._clock(now)
            self._period(now)
            self._expire(now)
            settings = self._settings()
            if not settings["enabled"] or settings["budget_paused"] or settings["clock_uncertain"] or self._seen(filtered_unit):
                return False
            if not self.eligible(filtered_unit["repository_id"], filtered_unit["event_time"], filtered_unit.get("event_end", filtered_unit["event_time"])):
                return False
            repo = self.db.execute("SELECT data FROM repositories WHERE id=?", (filtered_unit["repository_id"],)).fetchone()
            if json.loads(repo[0])["public_until"] <= now:
                return False
            if not filtered_unit.get("items"):
                self._mark(filtered_unit, "empty")
                return False
            size = len(_json(filtered_unit).encode())
            count, total = self.db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM pending_units").fetchone()
            local = self.db.execute("SELECT coalesce(sum(bytes),0) FROM pending_units WHERE repository_id=?", (filtered_unit["repository_id"],)).fetchone()[0]
            if count >= 10000 or total + size > 100 * 1024 * 1024 or local + size > 10 * 1024 * 1024:
                self._mark(filtered_unit, "queue_full")
                self._event("queue_full", "error", now, count=False)
                return False
            self._insert_unit(filtered_unit, now, now + DAY)
            return True

    def _unit(self, row):
        unit = json.loads(row["payload"])
        unit.update({k: row[k] for k in ("collected_at", "expires_at", "attempt_count", "state", "ready_at", "policy_revision")})
        return unit

    @_locked
    def pending(self, now):
        self.expire(now)
        return [self._unit(row) for row in self.db.execute("SELECT * FROM pending_units ORDER BY expires_at,repository_id,id")]

    @_locked
    def next_retry(self, now) -> float | None:
        """Return a retry deadline, excluding fresh, reserved and expired units."""
        return self.db.execute(
            "SELECT min(ready_at) FROM pending_units WHERE state='pending' "
            "AND expires_at>? AND ready_at<expires_at "
            "AND (attempt_count>0 OR ready_at>collected_at)", (now,)
        ).fetchone()[0]

    @_locked
    def invalidate_adapter(self, adapter, healthy, now):
        with self._transaction():
            self.db.execute("INSERT INTO adapters VALUES(?,?) ON CONFLICT(adapter) DO UPDATE SET healthy=excluded.healthy", (adapter, int(healthy)))
            self._event("adapter_" + adapter, "recovered" if healthy else "error", now)
            # Dispatch and settlement check each unit's adapter health. A source
            # failure must not cancel unrelated collection or generation.

    @_locked
    def split(self, unit_id, pieces, now, skipped_item_ids=None):
        with self._transaction():
            self._expire(now)
            row = self.db.execute("SELECT * FROM pending_units WHERE id=? AND state='pending'", (unit_id,)).fetchone()
            if not row:
                return []
            parent = json.loads(row["payload"])
            parent_items = {item["id"]: item for item in parent["items"]}
            used = set()
            normalized = []
            for piece in pieces:
                item_ids = [item["id"] for item in piece["items"]]
                if not item_ids or len(set(item_ids)) != len(item_ids) or any(i not in parent_items or i in used for i in item_ids):
                    raise ValueError("invalid_split")
                used.update(item_ids)
                value = dict(parent)
                value.update(piece)
                value["items"] = [parent_items[i] for i in item_ids]
                value["id"] = hashlib.sha256((parent["id"] + "\0" + "\0".join(item_ids)).encode()).hexdigest()
                value["repository_id"] = parent["repository_id"]
                value["equivalent_id"] = None
                value["partial_source"] = True
                normalized.append(value)
            skipped = set(skipped_item_ids or ())
            if skipped & used or not skipped <= parent_items.keys() or used | skipped != parent_items.keys():
                raise ValueError("incomplete_split")
            for item_id in skipped:
                marker = dict(parent)
                marker["id"] = hashlib.sha256((parent["id"] + "\0skipped\0" + item_id).encode()).hexdigest()
                marker["equivalent_id"] = None
                self._mark(marker, "oversized_item")
            self._mark(parent, "split")
            count, total = self.db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM pending_units").fetchone()
            local = self.db.execute("SELECT coalesce(sum(bytes),0) FROM pending_units WHERE repository_id=?", (parent["repository_id"],)).fetchone()[0]
            inserted = []
            for value in normalized:
                size = len(_json(value).encode())
                if count >= 10000 or total + size > 100 * 1024 * 1024 or local + size > 10 * 1024 * 1024:
                    self._mark(value, "queue_full")
                    self._event("queue_full", "error", now, count=False)
                    continue
                self._insert_unit(value, row["collected_at"], row["expires_at"], row["attempt_count"], row["malformed_count"], row["ready_at"])
                inserted.append(value)
                count += 1
                total += size
                local += size
            return [self._unit(self.db.execute("SELECT * FROM pending_units WHERE id=?", (p["id"],)).fetchone()) for p in inserted]

    @_locked
    def defer(self, unit_ids, now, retry_after, error):
        with self._transaction():
            self._expire(now)
            for identity in unit_ids:
                self.db.execute("UPDATE pending_units SET ready_at=max(ready_at,?) WHERE id=? AND state='pending'", (now + max(60, retry_after), identity))
            self._event(error, "error", now)

    @_locked
    def pause_generation(self, code, now):
        with self._transaction():
            self._pause_generation(code, now)

    def _pause_generation(self, code, now):
        settings = self._settings()
        settings.update(generation_paused=True, generation_pause_revision=settings["config_revision"], generation_pause_code=code)
        self._save_settings(settings)
        self._event(code, "paused", now)

    def _dispatch_allowed(self, unit_ids, epoch, now):
        settings = self._settings()
        if not unit_ids or not settings["enabled"] or settings["epoch"] != epoch or settings["budget_paused"] or settings["generation_paused"] or settings["clock_uncertain"]:
            return False
        for identity in unit_ids:
            row = self.db.execute("SELECT * FROM pending_units WHERE id=?", (identity,)).fetchone()
            if not row or row["expires_at"] <= now or row["ready_at"] > now:
                return False
            health = self.db.execute("SELECT healthy FROM adapters WHERE adapter=?", (row["adapter"],)).fetchone()
            repo = self.db.execute("SELECT data FROM repositories WHERE id=?", (row["repository_id"],)).fetchone()
            if (health and not health[0]) or not repo:
                return False
            repo = json.loads(repo[0])
            if repo["state"] != "public" or repo["public_until"] <= now:
                return False
            unit = json.loads(row["payload"])
            if not self.eligible(repo["id"], unit["event_time"], unit.get("event_end", unit["event_time"])):
                return False
        return True

    @_locked
    def dispatch_allowed(self, unit_ids, epoch, now):
        with self._transaction():
            self._clock(now)
            self._period(now)
            self._expire(now)
            return self._dispatch_allowed(unit_ids, epoch, now)

    @_locked
    def reserve(self, unit_ids, input_tokens, model, now, max_output_tokens=2000):
        with self._transaction():
            self._clock(now)
            period = self._period(now)
            self._expire(now)
            settings = self._settings()
            if model != MODEL:
                self._pause_generation("unknown_model_rate", now)
                return None
            if type(input_tokens) is not int or not 0 <= input_tokens <= 12000 or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2000:
                raise ValueError("invalid_token_reservation")
            if len(set(unit_ids)) != len(unit_ids) or not self._dispatch_allowed(unit_ids, settings["epoch"], now):
                return None
            if self.db.execute("SELECT 1 FROM attempts WHERE state='reserved' LIMIT 1").fetchone():
                return None
            rows = [self.db.execute("SELECT * FROM pending_units WHERE id=?", (i,)).fetchone() for i in unit_ids]
            if len({row["repository_id"] for row in rows}) != 1 or any(row["state"] != "pending" or row["attempt_count"] >= 4 for row in rows):
                return None
            amount = input_tokens + max_output_tokens * 5
            if period["spent"] + period["reserved"] + amount > self._cap(settings):
                settings["budget_paused"] = True
                self._save_settings(settings)
                self._open_interval("global", "budget", now)
                self._event("budget_paused", "paused", now)
                return None
            identity = str(uuid.uuid4())
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, _json(unit_ids), rows[0]["repository_id"], settings["epoch"], period["id"], model, 1, 5,
                 amount, input_tokens, max_output_tokens, now, "reserved", None, None, None,
                 settings["config_revision"], settings["config"].get("generation", {}).get("character_limit", 500)))
            self.db.execute("UPDATE periods SET reserved=reserved+? WHERE id=?", (amount, period["id"]))
            for row in rows:
                self.db.execute("UPDATE pending_units SET state='reserved',attempt_count=attempt_count+1 WHERE id=?", (row["id"],))
            return {"id": identity, "epoch": settings["epoch"], "period": period["id"],
                    "reservation_micro_usd": amount, "units": [
                        self._unit(self.db.execute("SELECT * FROM pending_units WHERE id=?", (row["id"],)).fetchone())
                        for row in rows]}

    @staticmethod
    def _source_ref(unit, item):
        reference = dict(unit.get("source_ref", {}))
        reference.update(item.get("source_ref", {}))
        if unit["kind"] == "commit":
            return {"commit": str(reference.get("commit", reference.get("commit_hash", unit["id"])))}
        # Explicit allowlist: never serialize collector file paths or arbitrary metadata.
        def public_identity(value):
            value = str(value)
            if value.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:[\\/]", value):
                return hashlib.sha256(value.encode()).hexdigest()
            return value
        message_ids = reference.get("message_ids", [reference.get("message_id", item["id"])])
        if not isinstance(message_ids, list):
            message_ids = [message_ids]
        return {"client": unit.get("adapter", "unknown"),
                "session_id": public_identity(reference.get("session_id", "")),
                "message_ids": [public_identity(value) for value in message_ids]}

    def _accept(self, attempt, units, candidates, now):
        evidence = {item["id"]: (unit, item) for unit in units for item in unit["items"]}
        repo = json.loads(self.db.execute("SELECT data FROM repositories WHERE id=?", (attempt["repository_id"],)).fetchone()[0])
        for candidate in candidates:
            identity = str(uuid.uuid4())
            record = {"schema_version": 1, "id": identity, "created_at": _utc(now),
                      "repository": {"id": repo["id"], "host": repo["host"], "path": repo["project_path"]},
                      "text": candidate["text"], "character_limit": attempt["character_limit"],
                      "generation": {"model": attempt["model"], "prompt_version": 1, "policy_revision": attempt["policy_revision"]}}
            cursor = self.db.execute("INSERT INTO suggestions(id,repository_id,created_at,record) VALUES(?,?,?,?)",
                                     (identity, repo["id"], now, "{}"))
            record["sequence"] = cursor.lastrowid
            self.db.execute("UPDATE suggestions SET record=? WHERE id=?", (_json(record), identity))
            for item_id in dict.fromkeys(candidate["evidence_ids"]):
                unit, item = evidence[item_id]
                entry = {"id": str(uuid.uuid4()), "kind": unit["kind"], "source_ref": self._source_ref(unit, item),
                         "occurred_at": _utc(item.get("event_time", unit["event_time"])),
                         "provenance": item["provenance"], "partial_source": bool(unit.get("partial_source", False)),
                         "excerpt": item["text"][:1000]}
                self.db.execute("INSERT INTO evidence VALUES(?,?,?)", (entry["id"], identity, _json(entry)))
            self.db.execute("INSERT INTO export_outbox VALUES(?,?,0)", (identity, record["sequence"]))

    @_locked
    def settle(self, attempt_id, usage, candidates, now, error=None, retry_after=0):
        with self._transaction():
            attempt = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["state"] != "reserved":
                return
            valid_usage = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
            charge = usage["input_tokens"] + usage["output_tokens"] * 5 if valid_usage else attempt["reservation"]
            self.db.execute("UPDATE periods SET reserved=reserved-?,spent=spent+? WHERE id=?", (attempt["reservation"], charge, attempt["period"]))
            self.db.execute("UPDATE attempts SET state='settled',usage=?,charge=?,estimated=? WHERE id=?",
                            (_json(usage) if valid_usage else None, charge, int(not valid_usage), attempt_id))
            self._clock(now)
            self._period(now)
            self._expire(now)
            ids = json.loads(attempt["unit_ids"])
            rows = [self.db.execute("SELECT * FROM pending_units WHERE id=?", (i,)).fetchone() for i in ids]
            rows = [row for row in rows if row]
            if error == "cancelled" and valid_usage and charge == 0:
                for row in rows:
                    self.db.execute("UPDATE pending_units SET state='pending',attempt_count=max(0,attempt_count-1) WHERE id=?", (row["id"],))
                self.db.execute("UPDATE attempts SET state='cancelled' WHERE id=?", (attempt_id,))
                return
            units = [json.loads(row["payload"]) for row in rows]
            allowed = len(rows) == len(ids) and self._dispatch_allowed(ids, attempt["epoch"], now)
            evidence_ids = {item["id"] for unit in units for item in unit["items"]}
            if allowed and error is None:
                valid = isinstance(candidates, list) and len(candidates) <= 3
                if valid:
                    valid = all(isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()
                        and len(c["text"]) <= attempt["character_limit"] and isinstance(c.get("evidence_ids"), list)
                        and 1 <= len(c["evidence_ids"]) <= 8 and all(isinstance(i, str) and i in evidence_ids for i in c["evidence_ids"]) for c in candidates)
                if not valid:
                    error = "malformed_output"
                else:
                    self._accept(attempt, units, candidates, now)
                    for unit in units:
                        self._mark(unit, "accepted" if candidates else "no_candidate")
                    self.db.execute("UPDATE attempts SET state=? WHERE id=?", ("accepted" if candidates else "no_candidate", attempt_id))
                    return
            permanent = error in ("authentication_error", "permission_error", "model_error")
            if permanent:
                self._pause_generation(error, now)
            retryable = error in ("network_error", "rate_limited", "server_error", "malformed_output", "cancelled", "crash_recovery") or permanent or not allowed
            for row, unit in zip(rows, units):
                malformed = row["malformed_count"] + int(error == "malformed_output")
                if not retryable or row["attempt_count"] >= 4 or malformed >= 2:
                    self._mark(unit, "generation_failed")
                else:
                    delay = (60, 300, 1800)[min(row["attempt_count"] - 1, 2)]
                    self.db.execute("UPDATE pending_units SET state='pending',malformed_count=?,ready_at=? WHERE id=?",
                                    (malformed, now + max(delay, retry_after), row["id"]))
            if error and error != "cancelled":
                self._event(error, "error", now)

    @_locked
    def mark_import_complete(self, repository_id, now):
        with self._transaction():
            row = self.db.execute("SELECT data FROM repositories WHERE id=?", (repository_id,)).fetchone()
            if row:
                repo = json.loads(row[0])
                repo["import_complete"] = True
                repo["import_completed_at"] = now
                self._save_repo(repo)

    @_locked
    def cursor(self, key):
        row = self.db.execute("SELECT value FROM source_cursors WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @_locked
    def save_cursor(self, key, value):
        with self._transaction():
            self.db.execute("INSERT INTO source_cursors VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _json(value)))

    @_locked
    def source_health(self):
        """Summarize stream checkpoints without exposing paths or transcript metadata."""
        result = {}
        for key, value in self.db.execute(
                "SELECT key,value FROM source_cursors WHERE key LIKE 'adapter-stream:%' ORDER BY key"):
            client = key.split(":", 2)[1]
            if client not in ("claude", "codex", "omp"):
                continue
            state = json.loads(value)
            summary = result.setdefault(client, {"streams": 0, "pending_turns": 0,
                                                 "quarantined_streams": 0, "errors": []})
            summary["streams"] += 1
            pending = state.get("pending_turns", 0)
            if type(pending) is int and pending > 0:
                summary["pending_turns"] += pending
            summary["quarantined_streams"] += int(bool(state.get("quarantine")))
            for field in ("quarantine", "error"):
                code = state.get(field)
                if not code:
                    continue
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,60}", code):
                    code = "source_error"
                if code not in summary["errors"]:
                    summary["errors"].append(code)
        for summary in result.values():
            summary["errors"].sort()
        return result

    @_locked
    def list_suggestions(self, repo=None, since=None, limit=20):
        if isinstance(since, str):
            since = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        return [json.loads(row[0]) for row in self.db.execute("SELECT record FROM suggestions WHERE (? IS NULL OR repository_id=?) "
            "AND (? IS NULL OR created_at>=?) ORDER BY sequence DESC LIMIT ?", (repo, repo, since, since, limit))]

    @_locked
    def show(self, id, evidence=False):
        row = self.db.execute("SELECT record FROM suggestions WHERE id=?", (id,)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if evidence:
            record["evidence"] = [json.loads(r[0]) for r in self.db.execute("SELECT record FROM evidence WHERE suggestion_id=? ORDER BY rowid", (id,))]
        return record

    def _export_records(self):
        return [self.show(row[0], evidence=True) for row in self.db.execute("SELECT id FROM suggestions ORDER BY sequence").fetchall()]

    def _sync_directory(self):
        fd = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @_locked
    def export(self):
        records = self._export_records()
        expected = [_json(record).encode("utf-8") + b"\n" for record in records]
        settings = self._settings()
        rebuild = settings["export_rebuild"] or not self.export_path.exists()
        prefix = 0
        if not rebuild:
            try:
                with self.export_path.open("rb") as stream:
                    for index, line in enumerate(stream):
                        if index >= len(expected) or line != expected[index]:
                            rebuild = True
                            break
                        prefix += 1
            except OSError:
                self.event("export_failed")
                raise
            if rebuild:
                self.event("export_corrupt")
        try:
            if rebuild:
                temporary = self.data_dir / "suggestions.jsonl.tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    for line in expected:
                        stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.export_path)
                self._sync_directory()
            elif prefix < len(expected):
                with self.export_path.open("ab") as stream:
                    for line in expected[prefix:]:
                        stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            with self._transaction():
                self.db.execute("UPDATE export_outbox SET exported=1")
                settings = self._settings()
                settings["export_rebuild"] = False
                self._save_settings(settings)
            self._permissions()
        except OSError:
            self.event("export_failed")
            raise

    @_locked
    def purge(self, repository_id, now):
        with self._transaction():
            settings = self._settings()
            settings["epoch"] += 1
            settings["export_rebuild"] = True
            self._save_settings(settings)
            rows = self.db.execute("SELECT payload FROM pending_units WHERE (? IS NULL OR repository_id=?)", (repository_id, repository_id)).fetchall()
            for row in rows:
                self._mark(json.loads(row[0]), "purged")
            count = self.db.execute("SELECT count(*) FROM suggestions WHERE (? IS NULL OR repository_id=?)", (repository_id, repository_id)).fetchone()[0]
            self.db.execute("DELETE FROM suggestions WHERE (? IS NULL OR repository_id=?)", (repository_id, repository_id))
            self._counter("purged")
        try:
            self.export()
        finally:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"suggestions": count, "pending": len(rows), "repository_id": repository_id}

    @_locked
    def recover(self, now):
        self.expire(now)
        attempts = self.db.execute("SELECT id FROM attempts WHERE state='reserved'").fetchall()
        for attempt in attempts:
            self.settle(attempt[0], None, None, now, error="crash_recovery")
        self.export()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @_locked
    def status(self, now):
        self.expire(now)
        settings = self._settings()
        period = dict(self.db.execute("SELECT * FROM periods WHERE id=?", (settings["period"],)).fetchone())
        queue = dict(self.db.execute("SELECT count(*) AS count,coalesce(sum(bytes),0) AS bytes,min(expires_at) AS next_expiry FROM pending_units").fetchone())
        return {"enabled": settings["enabled"], "epoch": settings["epoch"], "config_revision": settings["config_revision"],
                "clock_uncertain": settings["clock_uncertain"], "generation_paused": settings["generation_paused"],
                "generation_pause_code": settings["generation_pause_code"], "queue": queue,
                "budget_paused": settings["budget_paused"], "budget": {"period": period["id"], "timezone": period["timezone"],
                    "spent_micro_usd": period["spent"], "reserved_micro_usd": period["reserved"], "cap_micro_usd": self._cap(settings),
                    "resume_at": period["end"] if settings["budget_paused"] else None},
                "repositories": self.repositories(), "adapters": {r[0]: bool(r[1]) for r in self.db.execute("SELECT * FROM adapters")},
                "counters": {r[0]: r[1] for r in self.db.execute("SELECT * FROM counters")},
                "events": [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")],
                "export": {"lag": self.db.execute("SELECT count(*) FROM export_outbox WHERE exported=0").fetchone()[0],
                           "rebuild_required": settings["export_rebuild"]}}

    @_locked
    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()
