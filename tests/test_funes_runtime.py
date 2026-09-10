"""Actual fork subprocess, Git objects, SQLite and generation lifecycle; synthetic only."""
import copy
import json

import httpx
import pytest

from actomasto import daemon
from actomasto.config import validate
from actomasto.funes_source import FunesSource
from actomasto.generation import Generator, MODEL
from actomasto.policy import Policy
from actomasto.store import Store
from test_funes_source import Source, START
from test_sources import GitFixture

NOW = START + 10


@pytest.fixture
def integrated(tmp_path, funes_bin, monkeypatch):
    source = Source(tmp_path, 'omp', funes_bin)
    source.repo.rmdir()
    git = GitFixture(source.repo)
    git.run('remote', 'add', 'origin', 'https://github.com/example/project.git')
    git.commit({'code.py': 'value = 1\n'}, timestamp=int(START + 5), message='Add deterministic cache fixture')
    config = validate({'discovery': {'roots': [str(tmp_path)]},
                       'identity': {'author_emails': ['author@example.test']}, 'funes': source.config,
                       'notifications': {'enabled': False}})
    store = Store(tmp_path / 'state')
    store.apply_config(config, NOW)
    store.set_enabled(True, NOW)
    monkeypatch.setattr(daemon, 'active_login', lambda: True)
    monkeypatch.setattr(daemon.time, 'time', lambda: NOW)
    monkeypatch.setattr(daemon, 'verify', lambda origin: {'state': 'public', 'id': 'github.com:1'})
    runtime = daemon.Runtime(store)
    source.refresh()
    yield runtime, source, git, config
    runtime.store.close()


def test_actual_fork_collection_restart_and_git_during_outage(integrated):
    runtime, source, git, _ = integrated
    runtime.collect()
    pending = runtime.store.pending(NOW)
    assert {unit['kind'] for unit in pending} == {'commit', 'conversation'}
    conversation = next(unit for unit in pending if unit['kind'] == 'conversation')
    original_expiry = conversation['expires_at']
    runtime.collect()
    assert [(unit['id'], unit['expires_at']) for unit in runtime.store.pending(NOW)] == [
        (unit['id'], unit['expires_at']) for unit in pending]
    runtime.store.close()
    runtime.store = Store(source.repo.parent / 'state')
    assert not runtime.store.dispatch_allowed([conversation['id']], runtime.store.settings()['epoch'], NOW)
    runtime.collect()
    assert runtime.store.dispatch_allowed([conversation['id']], runtime.store.settings()['epoch'], NOW)
    source.config['executable'] = '/missing/funes'
    cfg = copy.deepcopy(runtime.store.settings()['config'])
    cfg['funes']['executable'] = '/missing/funes'
    runtime.store.apply_config(cfg, NOW)
    latest = git.commit({'code.py': 'value = 2\n'}, timestamp=int(START + 6), message='Update cache fixture independently')
    runtime.collect()
    pending = runtime.store.pending(NOW)
    assert any(unit['source_ref'].get('commit') == latest for unit in pending if unit['kind'] == 'commit')
    retained = next(unit for unit in pending if unit['id'] == conversation['id'])
    assert retained['expires_at'] == original_expiry
    assert not runtime.store.dispatch_allowed([conversation['id']], runtime.store.settings()['epoch'], NOW)


def test_entire_original_turn_passes_through_literal_filter(integrated):
    runtime, source, _, config = integrated
    source.records[-1]['message']['content'][0]['text'] += ' Never transmit the cobalt customer phrase.'
    source.write(source.records)
    source.refresh()
    config['blocklist']['text'] = ['cobalt customer phrase']
    runtime.store.apply_config(config, NOW)
    runtime.collect()
    assert all(unit['kind'] != 'conversation' for unit in runtime.store.pending(NOW))
    marker = runtime.store.db.execute("SELECT reason FROM source_markers WHERE reason='blocked_text'").fetchall()
    assert marker
    runtime.collect()
    assert all(unit['kind'] != 'conversation' for unit in runtime.store.pending(NOW))


def test_scope_change_between_gate_and_read_never_queues_content(integrated, monkeypatch):
    runtime, source, _, _ = integrated
    request = FunesSource.request
    def revoke_scope(self, op, **fields):
        if op == 'read':
            scope = json.loads(open(source.config['scope']).read())
            scope['roots']['omp'] = []
            with open(source.config['scope'], 'w') as stream:
                json.dump(scope, stream)
        return request(self, op, **fields)
    monkeypatch.setattr(FunesSource, 'request', revoke_scope)
    runtime.collect()
    assert all(unit['kind'] != 'conversation' for unit in runtime.store.pending(NOW))
    assert runtime.store.cursor('funes-status:omp')['error'] == 'scope_changed'


def test_dependency_is_rechecked_after_provider_response(integrated):
    runtime, source, _, config = integrated
    runtime.collect()
    conversation = next(unit for unit in runtime.store.pending(NOW) if unit['kind'] == 'conversation')
    for unit in runtime.store.pending(NOW):
        if unit['kind'] == 'commit':
            runtime.store.mark(unit, 'test_git_already_processed')
    def provider(request):
        if request.url.path.endswith('count_tokens'):
            return httpx.Response(200, json={'input_tokens': 100})
        broken = copy.deepcopy(source.records)
        broken[1]['version'] = 999
        source.write(broken)
        source.refresh()
        return httpx.Response(200, json={'model': MODEL, 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 100, 'output_tokens': 20},
            'content': [{'type': 'text', 'text': json.dumps({'candidates': [
                {'text': 'I fixed cache invalidation.', 'evidence_ids': [conversation['items'][-1]['id']]}]})}]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        generator = Generator(runtime.store, config, Policy(config), runtime.visibility,
                              api_key='synthetic-key', client=client, clock=lambda: NOW,
                              source_check=runtime.source_check)
        result = generator.cycle(NOW)
    assert result['generation_requests'] == 1 and result['candidates'] == 0
    assert runtime.store.list_suggestions() == []
    assert runtime.store.cursor('funes-status:omp')['error'] == 'unsupported_version'
    attempt = runtime.store.db.execute('SELECT charge FROM attempts').fetchone()
    assert attempt['charge'] == 200


def test_scope_revoked_during_filter_never_persists_turn(integrated, monkeypatch):
    runtime, source, _, _ = integrated
    original = Policy.filter
    def revoke(self, unit):
        filtered = original(self, unit)
        if unit['kind'] == 'conversation':
            with open(source.config['scope'], 'w') as stream:
                json.dump({'version': 1, 'roots': {'claude': [], 'codex': [], 'omp': []}}, stream)
        return filtered
    monkeypatch.setattr(Policy, 'filter', revoke)
    runtime.collect()
    assert all(unit['kind'] != 'conversation' for unit in runtime.store.pending(NOW))
    assert runtime.store.cursor('funes-status:omp')['error'] == 'scope_changed'


def test_scope_revoked_during_visibility_prevents_provider_dispatch(integrated):
    runtime, source, _, config = integrated
    runtime.collect()
    for unit in runtime.store.pending(NOW):
        if unit['kind'] == 'commit':
            runtime.store.mark(unit, 'test_git_already_processed')
    def visibility(repository):
        assert runtime.visibility(repository)
        with open(source.config['scope'], 'w') as stream:
            json.dump({'version': 1, 'roots': {'claude': [], 'codex': [], 'omp': []}}, stream)
        return True
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('revoked source transmitted'))) as client:
        generator = Generator(runtime.store, config, Policy(config), visibility,
                              api_key='synthetic-key', client=client, clock=lambda: NOW,
                              source_check=runtime.source_check)
        result = generator.cycle(NOW)
    assert result['count_requests'] == result['generation_requests'] == 0
    assert runtime.store.list_suggestions() == []
