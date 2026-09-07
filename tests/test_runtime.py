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


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for key, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'runtime')]:
        monkeypatch.setenv(key, str(tmp_path / sub))
    monkeypatch.setattr(daemon, 'active_login', lambda: True)
    store = Store(tmp_path / 'data' / 'actomasto')
    config = validate({'discovery': {'roots': [str(tmp_path / 'roots')]}, 'identity': {'author_emails': ['author@example.invalid']},
                       'notifications': {'enabled': False}, 'sources': {name + '_root': str(tmp_path / name) for name in ('claude', 'codex', 'omp')}})
    store.apply_config(config, time.time())
    app = daemon.Runtime(store)
    yield app, config
    store.close()


def test_cli_init_requires_consent_and_stays_disabled(tmp_path, monkeypatch, capsys):
    for key, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'runtime')]:
        monkeypatch.setenv(key, str(tmp_path / sub))
    assert cli.main(['init', '--root', str(tmp_path / 'roots'), '--author-email', 'author@example.invalid', '--accept-hosted-processing']) == 0
    assert json.loads(capsys.readouterr().out)['enabled'] is False
    assert cli.main(['status', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert not status['enabled'] and not status['process_running'] and not status['repositories']
    assert cli.main(['show', 'missing', '--json']) == 3
    assert json.loads(capsys.readouterr().out)['error'] == 'suggestion_not_found'


def test_config_round_trip_and_unknown_keys(runtime, tmp_path):
    _, config = runtime
    config['blocklist']['scoped'] = [{'repository': 'github.com/example/**', 'paths': ['private/**'], 'text': ['customer phrase']}]
    path = tmp_path / 'private-config' / 'config.toml'
    save(path, config)
    assert load(path) == config
    assert path.stat().st_mode & 0o777 == 0o600
    invalid = copy.deepcopy(config)
    invalid['identity']['committer_names'] = ['Someone']
    with pytest.raises(ConfigError):
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


def test_successful_collection_continues_to_all_adapters(runtime, monkeypatch):
    app, _ = runtime
    app.store.set_enabled(True, time.time())
    monkeypatch.setattr(daemon, 'discover', lambda roots: [])
    called = []
    def adapters(client, *args, **kwargs):
        called.append(client)
        return iter(())
    monkeypatch.setattr(daemon, 'conversations', adapters)
    app.collect()
    assert called == ['claude', 'codex', 'omp']
    assert app.store.status(time.time())['adapters'] == {'claude': True, 'codex': True, 'omp': True}


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
