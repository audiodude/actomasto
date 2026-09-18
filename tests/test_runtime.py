"""Observable CLI and control boundaries using isolated synthetic state."""
import copy
import json
import subprocess
import threading
import time

import pytest

from actomasto import cli, daemon
from actomasto.common import SourceCancelled, terminal_safe
from actomasto.config import ConfigError, load, save, validate
from actomasto.store import Store
from actomasto import discovery


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for key, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'runtime')]:
        monkeypatch.setenv(key, str(tmp_path / sub))
    monkeypatch.setattr(daemon, 'active_login', lambda: True)
    store = Store(tmp_path / 'data' / 'actomasto')
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'version': 1, 'roots': {name: [] for name in ('claude', 'codex', 'omp')}}))
    scope.chmod(0o600)
    config = validate({'discovery': {'roots': [str(tmp_path / 'roots')]}, 'identity': {'author_emails': ['author@example.invalid']},
                       'notifications': {'enabled': False},
                       'funes': {'executable': '/missing/funes', 'corpus': str(tmp_path / 'corpus'), 'scope': str(scope)}})
    store.apply_config(config, time.time())
    app = daemon.Runtime(store)
    yield app, config
    store.close()


def test_cli_init_requires_consent_and_stays_disabled(tmp_path, monkeypatch, capsys, funes_bin):
    for key, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'runtime')]:
        monkeypatch.setenv(key, str(tmp_path / sub))
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'version': 1, 'roots': {name: [] for name in ('claude', 'codex', 'omp')}}))
    scope.chmod(0o600)
    assert cli.main(['init', '--root', str(tmp_path / 'roots'), '--author-email', 'author@example.invalid',
                     '--funes-bin', funes_bin, '--funes-corpus', str(tmp_path / 'corpus'),
                     '--funes-scope', str(scope), '--accept-hosted-processing']) == 0
    assert json.loads(capsys.readouterr().out)['enabled'] is False
    assert cli.main(['status', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert not status['enabled'] and not status['process_running'] and not status['repositories']
    assert cli.main(['show', 'missing', '--json']) == 3
    assert json.loads(capsys.readouterr().out)['error'] == 'suggestion_not_found'


def test_config_round_trip_and_unknown_keys(runtime, tmp_path):
    _, config = runtime
    config['blocklist']['scoped'] = [{'repository': 'github.com/example/**', 'paths': ['private/**'], 'text': ['customer phrase']}]
    config['git']['since'] = '2026-01-01'
    path = tmp_path / 'private-config' / 'config.toml'
    save(path, config)
    assert load(path) == config
    assert path.stat().st_mode & 0o777 == 0o600
    invalid = copy.deepcopy(config)
    invalid['identity']['committer_names'] = ['Someone']
    with pytest.raises(ConfigError):
        validate(invalid)
    invalid = copy.deepcopy(config)
    invalid['git']['since'] = '2026-02-30'
    with pytest.raises(ConfigError, match='invalid_git_since'):
        validate(invalid)


def test_off_waits_for_source_stop_before_acknowledgment(runtime, monkeypatch):
    app, _ = runtime
    app.store.set_enabled(True, time.time())
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    epoch = app.store.settings()['epoch']
    def source():
        with app.source_lock:
            entered.set()
            while app.store.settings()['epoch'] == epoch:
                release.wait(.005)
            cancelled.set()
    thread = threading.Thread(target=source)
    thread.start()
    assert entered.wait(1)
    assert app.command({'command': 'off'}) == {'enabled': False}
    assert cancelled.is_set()
    thread.join(1)
    assert not thread.is_alive()


def test_logout_suspends_without_explicit_off_interval(runtime, monkeypatch):
    app, _ = runtime
    app.store.set_enabled(True, time.time())
    epoch = app.store.settings()['epoch']
    monkeypatch.setattr(daemon, 'active_login', lambda: False)
    monkeypatch.setattr(daemon, 'discover', lambda roots: pytest.fail('must not inspect roots without login'))
    app.work()
    assert not app.allowed()
    assert app.store.settings()['enabled'] and app.store.settings()['epoch'] == epoch


@pytest.mark.parametrize("boundary", ["off", "logout", "configuration"])
def test_scan_cancels_during_discovery_without_publishing_partial_state(runtime, tmp_path, monkeypatch, boundary):
    app, config = runtime
    root = tmp_path / "roots"
    root.mkdir()
    app.store.set_enabled(True, time.time())
    previous = {"candidates": [], "at": 123}
    app.store.save_cursor("discovery_status", previous)
    clock = [0.]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    original_scandir = discovery.os.scandir

    def scandir(fd):
        if boundary == "off":
            app.store.set_enabled(False, time.time())
        elif boundary == "logout":
            monkeypatch.setattr(daemon, "active_login", lambda: False)
        else:
            changed = copy.deepcopy(config)
            changed["identity"]["author_emails"].append("new@example.invalid")
            app.store.apply_config(changed, time.time())
        clock[0] += 1.
        return original_scandir(fd)

    monkeypatch.setattr(discovery.os, "scandir", scandir)
    with pytest.raises(SourceCancelled):
        app.scan(force=True)
    assert app.store.cursor("discovery_status") == previous
    assert app.store.repositories() == []




def test_notification_failure_is_visible_without_loop(runtime, monkeypatch):
    app, config = runtime
    config['notifications']['enabled'] = True
    app.store.apply_config(config, time.time())
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(args[0])
        raise FileNotFoundError()
    monkeypatch.setattr(daemon.subprocess, 'run', unavailable)
    app.store.event('fixture_failure', 'error')
    app.maintenance()
    app.maintenance()
    assert len(calls) == 1
    events = app.store.status(time.time())['events']
    assert any(row['code'] == 'notification_delivery' and row['state'] == 'error' for row in events)
    assert all('fixture_failure' not in argument for call in calls for argument in call)


def test_terminal_controls_are_escaped():
    assert terminal_safe('draft\x1b]52;clipboard\x07\u202e') == 'draft\\u001b]52;clipboard\\u0007\\u202e'


def test_logind_lingering_manager_without_sessions_is_not_login(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout='\n'))
    assert not daemon.active_login()


def test_logind_real_user_session_permits_collection(monkeypatch):
    def response(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout='42\n' if 'show-user' in args else 'Class=user\nState=active\n')
    monkeypatch.setattr(daemon.subprocess, 'run', response)
    assert daemon.active_login()
