from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .common import SourceCancelled, credential, locations, secure_dir, writer_lock
from .adapters import AdapterError, collect as conversations
from .config import validate
from .discovery import discover, verify
from .generation import Generator
from .git_source import GitSourceError, collect as commits
from .policy import Policy
from .store import Store


def active_login():
    """logind distinguishes lingering user managers from real login sessions."""
    try:
        result = subprocess.run(['loginctl', 'show-user', str(os.getuid()), '--property=Sessions', '--value'],
                                capture_output=True, text=True, timeout=5, check=True)
        for session in result.stdout.split():
            properties = subprocess.run(['loginctl', 'show-session', session, '--property=Class', '--property=State'],
                                        capture_output=True, text=True, timeout=5, check=True).stdout
            fields = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
            if fields.get('Class') in ('user', 'user-early') and fields.get('State') in ('active', 'online'):
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


class Runtime:
    def __init__(self, store):
        self.store = store
        self.scan_lock = threading.RLock()
        self.next_scan = 0
        self.source_lock = threading.RLock()
        self.next_generation = 0
        self.stop = False
        self.generator = None
        self.config_revision = None

    def allowed(self):
        settings = self.store.settings()
        return (not self.stop and settings['enabled'] and not settings.get('budget_paused')
                and not settings.get('clock_uncertain') and active_login())

    def visibility(self, repository_id):
        if not self.allowed():
            return False
        # Recheck local origin identity even while the public API cache is valid.
        self.scan(force=True)
        return any(r['id'] == repository_id and r['state'] == 'public' and r['public_until'] > time.time()
                   for r in self.store.repositories()) and self.allowed()

    def scan(self, force=False):
        with self.scan_lock:
            if not self.allowed():
                return
            now = time.time()
            if not force and now < self.next_scan:
                return
            settings = self.store.settings()
            config = settings['config']
            policy = Policy(config)
            discovered = discover(config['discovery']['roots'])
            results = {}
            for entry in discovered:
                origin = entry.get('origin')
                if not origin or entry.get('reason') or policy.repository_blocked(origin):
                    continue
                if not self.allowed():
                    return
                cache = self.store.cursor('visibility:' + origin) or {}
                if cache.get('state') == 'public' and cache.get('expires_at', 0) > now:
                    results[origin] = cache
                elif cache.get('next_retry', 0) > now:
                    results[origin] = {'state': 'uncertain', 'reason': 'visibility_backoff'}
                else:
                    result = verify(origin)
                    if result['state'] == 'public':
                        result.update(checked_at=now, expires_at=now + 86400, failures=0)
                    else:
                        failures = cache.get('failures', 0) + 1
                        result.update(failures=failures, next_retry=now + max(result.get('retry_after', 0), [60, 300, 1800][min(failures - 1, 2)]))
                    self.store.save_cursor('visibility:' + origin, result)
                    results[origin] = result
            with self.store.lock:
                if self.store.settings()['epoch'] != settings['epoch'] or not self.allowed():
                    return
                self.store.sync_repositories(discovered, results, time.time())
                self.store.save_cursor('discovery_status', {'candidates': discovered, 'at': now})
            retry_times = [result['next_retry'] for result in results.values() if result.get('next_retry', 0) > now]
            self.next_scan = min([now + 300, *retry_times])

    def accept(self, unit, repository, epoch):
        with self.store.lock:
            if not self.allowed() or self.store.settings()['epoch'] != epoch:
                return False
            now = time.time()
            self.store.expire(now)
            if self.store.seen(unit):
                return True
            if unit.get('exclusion_reason'):
                self.store.mark(unit, unit['exclusion_reason'])
                return True
            if unit['event_end'] > now:
                return True
            if not self.store.eligible(unit['repository_id'], unit['event_time'], unit['event_end']):
                self.store.mark(unit, 'ineligible_interval')
                return True
            unit['repository_display'] = repository['display_path']
            policy = Policy(self.store.settings()['config'])
            filtered = policy.filter(unit)
            if filtered is None:
                self.store.mark(unit, policy.last_reason or 'blocked')
            else:
                self.store.enqueue(filtered, now)
            return True

    def collect(self):
        with self.source_lock:
            return self._collect()

    def _collect(self):
        self.scan(force=True)
        if not self.allowed():
            return
        settings = self.store.settings()
        epoch, config = settings['epoch'], settings['config']
        def cancelled():
            if not self.allowed() or self.store.settings()['epoch'] != epoch:
                raise SourceCancelled()
        repos = [r for r in self.store.repositories() if r['state'] == 'public' and r['public_until'] > time.time()]
        for repo in repos:
            complete = True
            for path in repo['paths']:
                cancelled()
                try:
                    last_unit = None
                    for unit in commits(repo, path, config['identity']['author_emails'],
                                        lambda start, end: self.store.eligible(repo['id'], start, end),
                                        cancelled=cancelled):
                        if not self.accept(unit, repo, epoch):
                            return
                        last_unit = unit['id']
                    with self.store.lock:
                        cancelled()
                        self.store.save_cursor('git-path:' + hashlib.sha256(path.encode()).hexdigest(),
                                               {'adapter': 'git', 'repository_id': repo['id'],
                                                'last_unit_id': last_unit, 'last_success': time.time()})
                    self.store.event('git_collection', 'ok')
                except GitSourceError:
                    complete = False
                    self.store.event('git_collection', 'error')
            if complete:
                self.store.mark_import_complete(repo['id'], time.time())
        by_id = {r['id']: r for r in repos}
        associated = list(repos)
        known_paths = {path for repo in repos for path in repo['paths']}
        for entry in (self.store.cursor('discovery_status') or {}).get('candidates', []):
            if entry['path'] not in known_paths:
                associated.append({'id': 'ineligible:' + entry['path'], 'paths': [entry['path']]})
        for client in ('claude', 'codex', 'omp'):
            cancelled()
            try:
                for unit in conversations(client, Path(config['sources'][client + '_root']).expanduser(), associated,
                                          self.store.cursor, self.store.save_cursor, self.store.eligible,
                                          cancelled=cancelled):
                    if not self.accept(unit, by_id[unit['repository_id']], epoch):
                        return
                self.store.invalidate_adapter(client, True, time.time())
            except AdapterError:
                self.store.invalidate_adapter(client, False, time.time())

    def work(self):
        try:
            if not self.allowed():
                return
            now = time.time()
            if now >= self.next_scan:
                self.collect()
            settings = self.store.settings()
            if now >= self.next_generation and self.allowed():
                if self.config_revision != settings['config_revision']:
                    if self.generator is not None and hasattr(self.generator, 'close'):
                        self.generator.close()
                    self.generator = Generator(self.store, settings['config'], Policy(settings['config']), self.visibility, credential(), clock=time.time)
                    self.config_revision = settings['config_revision']
                result = self.generator.cycle(time.time())
                if result.get('candidates'):
                    self.notify(f"{result['candidates']} new drafts available", 'drafts')
                self.next_generation = time.time() + settings['config']['generation']['interval_minutes'] * 60
            retry = self.store.next_retry(time.time())
            if retry is not None:
                self.next_generation = min(self.next_generation, retry)
            self.store.event('runtime_work', 'ok')
        except SourceCancelled:
            return
        except Exception:
            # Never print exception messages: subprocess/provider errors may echo content.
            self.store.event('runtime_work', 'error')

    def notify(self, message, key):
        if not self.store.settings()['config'].get('notifications', {}).get('enabled', True):
            return
        try:
            subprocess.run(['notify-send', '--app-name=Actomasto', 'Actomasto', message],
                           capture_output=True, timeout=5, check=True)
            self.store.event('notification_delivery', 'ok')
        except (OSError, subprocess.SubprocessError):
            self.store.event('notification_delivery', 'error')

    def maintenance(self):
        with self.store.lock:
            self.store.expire(time.time())
            try:
                self.store.export()
                self.store.event('export_failure', 'ok')
            except (OSError, RuntimeError):
                self.store.event('export_failure', 'error')
            status = self.store.status(time.time())
        for event in reversed(status['events']):
            code = event.get('code', '')
            key = 'notified:' + code
            state = event.get('state')
            prior = self.store.cursor(key)
            if prior and prior.get('id', 0) >= event['id']:
                continue
            print(json.dumps({'event': code, 'state': state}), flush=True)
            if code != 'notification_delivery' and (state != 'ok' or prior):
                self.notify('An operational condition changed; run actomasto status.', key)
            self.store.save_cursor(key, {'id': event['id'], 'state': state})

    def command(self, request):
        command = request['command']
        with self.store.lock:
            now = time.time()
            if command == 'status':
                return {**self.store.status(now), 'process_running': True, 'active_login': active_login(),
                        'sources': self.store.source_health()}
            if command == 'repos':
                return {'repositories': self.store.repositories(), 'discovery': self.store.cursor('discovery_status')}
            if command == 'list':
                return self.store.list_suggestions(request.get('repo'), request.get('since'), request.get('limit', 20))
            if command == 'show':
                return self.store.show(request['id'], request.get('evidence', False))
            if command in ('on', 'off'):
                if command == 'on':
                    validate(self.store.settings()['config'])
                self.store.set_enabled(command == 'on', now)
                result = {'enabled': command == 'on'}
            elif command == 'apply':
                self.store.apply_config(validate(request['config']), now)
                result = {'config_revision': self.store.settings()['config_revision']}
            elif command == 'purge':
                result = self.store.purge(request.get('repo'), now)
            else:
                raise ValueError('unknown_control_command')
        # Gate closes above; acknowledge only after prior readers/requests stop.
        # Never hold the SQLite lock while waiting for either worker boundary.
        for barrier in (self.store.dispatch_lock, self.source_lock):
            while not barrier.acquire(timeout=1):
                with self.store.lock:
                    self.store.expire(time.time())
            barrier.release()
        self.next_scan = self.next_generation = 0
        return result


def run():
    os.umask(0o077)
    paths = locations()
    with writer_lock(paths['data']):
        store = Store(paths['data'])
        store.recover(time.time())
        runtime = Runtime(store)
        directory = secure_dir(paths['runtime'])
        socket_path = directory / 'control.sock'
        socket_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        server.listen(8)
        server.settimeout(1)
        print(json.dumps({'event': 'ready', 'enabled': store.settings()['enabled']}), flush=True)
        def stop(signum, frame):
            runtime.stop = True
        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = None
        maintenance = 0
        try:
            while not runtime.stop:
                if time.monotonic() >= maintenance:
                    runtime.maintenance()
                    maintenance = time.monotonic() + 30
                if future is None or future.done():
                    future = worker.submit(runtime.work)
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(2)
                    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                    try:
                        data = bytearray()
                        while not data.endswith(b'\n'):
                            part = connection.recv(65536)
                            if not part or len(data) + len(part) > 1024 * 1024:
                                raise ValueError('invalid_control_request')
                            data.extend(part)
                        result = {'result': runtime.command(json.loads(data))}
                    except Exception:
                        result = {'error': 'control_command_failed'}
                    try:
                        connection.sendall(json.dumps(result).encode() + b'\n')
                    except (BrokenPipeError, ConnectionResetError, socket.timeout):
                        pass
        finally:
            runtime.stop = True
            worker.shutdown(wait=True, cancel_futures=True)
            server.close()
            socket_path.unlink(missing_ok=True)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            store.close()
