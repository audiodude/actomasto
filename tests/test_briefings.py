"""Consumer-visible report budget and at-most-once delivery boundaries."""
from datetime import date
import json

import httpx
import pytest

from actomasto import briefings


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    for key, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data')]:
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-provider-credential')
    monkeypatch.setenv('MAILGUN_API_KEY', 'test-mail-credential')
    config = {'version': 1, 'roots': [str(tmp_path / 'projects')],
              'author_emails': ['author@example.invalid'], 'timezone': 'America/Los_Angeles',
              'hosted_processing': True, 'monthly_usd': '1.00',
              'mailgun': {'domain': 'mail.example.invalid', 'sender': 'reports@example.invalid',
                          'recipient': 'reader@example.invalid', 'region': 'US'}}
    return config


def fake_generation(monkeypatch):
    from actomasto import briefing_sources, briefing_generation
    monkeypatch.setattr(briefing_sources, 'collect', lambda *a, **kw: {'projects': [], 'coverage': ['Mac unavailable']})
    monkeypatch.setattr(briefing_generation, 'generate', lambda *a, **kw: {
        'subject': 'Daily report', 'text': 'No attributable activity found; Mac unavailable.',
        'usage': {'input_tokens': 100, 'output_tokens': 20}, 'model': 'claude-haiku-4-5-20251001'})


def test_ambiguous_delivery_is_not_repeated(report_env, monkeypatch):
    fake_generation(monkeypatch)
    calls = []
    def ambiguous(*a, **kw):
        calls.append(True)
        return {'state': 'unknown'}
    monkeypatch.setattr(briefings, '_send', ambiguous)
    first = briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24), send=True)
    again = briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24), send=True)
    assert first['delivery']['state'] == again['delivery']['state'] == 'unknown'
    assert calls == [True]


def test_crash_during_delivery_leaves_nonretryable_state(report_env, monkeypatch):
    fake_generation(monkeypatch)
    def crash(*a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(briefings, '_send', crash)
    with pytest.raises(KeyboardInterrupt):
        briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24), send=True)
    monkeypatch.setattr(briefings, '_send', lambda *a, **kw: pytest.fail('must not send again'))
    record = briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24), send=True)
    assert record['delivery']['state'] == 'sending'


def test_generated_report_can_be_sent_once_without_regeneration(report_env, monkeypatch):
    fake_generation(monkeypatch)
    from actomasto import briefing_generation
    report = briefings.run(report_env, kind='weekly', report_date=date(2026, 9, 24))
    assert report['delivery']['state'] == 'not_sent'
    monkeypatch.setattr(briefing_generation, 'generate', lambda *a, **kw: pytest.fail('must use prepared report'))
    calls = []
    monkeypatch.setattr(briefings, '_send', lambda *a, **kw: calls.append(True) or {'state': 'accepted'})
    for _ in range(2):
        report = briefings.run(report_env, kind='weekly', report_date=date(2026, 9, 24), send=True)
    assert report['delivery']['state'] == 'accepted'
    assert calls == [True]


def test_ambiguous_generation_retains_reservation(report_env, monkeypatch):
    fake_generation(monkeypatch)
    from actomasto import briefing_generation
    def failed(*a, **kw):
        raise RuntimeError('provider_disconnected')
    monkeypatch.setattr(briefing_generation, 'generate', failed)
    with pytest.raises(RuntimeError, match='provider_disconnected'):
        briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24))
    state = briefings._read_state(briefings._root())
    assert state['charges']['daily-2026-09-24']['micro_usd'] == briefings.RESERVATION
    with pytest.raises(briefings.BriefingError, match='generation_already_attempted'):
        briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24))


def test_budget_blocks_before_source_reads(report_env, monkeypatch):
    from actomasto import briefing_sources
    report_env['monthly_usd'] = '0.01'
    monkeypatch.setattr(briefing_sources, 'collect', lambda *a, **kw: pytest.fail('budget closed'))
    with pytest.raises(briefings.BriefingError, match='monthly_budget_exhausted'):
        briefings.run(report_env, kind='daily')


def test_disabled_hosted_processing_does_not_call_sources(report_env, monkeypatch):
    from actomasto import briefing_sources
    report_env['hosted_processing'] = False
    monkeypatch.setattr(briefing_sources, 'collect', lambda *a, **kw: pytest.fail('not authorized'))
    with pytest.raises(briefings.BriefingError, match='not_authorized'):
        briefings.run(report_env, kind='daily')


def test_mailgun_rejects_redirect_without_forwarding_credentials(report_env):
    calls = []
    def transport(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={'location': 'https://attacker.invalid/steal'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = briefings._send({'subject': 'Report', 'text': 'Evidence'}, report_env, 'key', client=client)
    assert result == {'state': 'rejected', 'http_status': 302}
    assert calls == ['api.mailgun.net']


def test_mailgun_test_mode_does_not_claim_delivery(report_env):
    from urllib.parse import parse_qs
    def transport(request):
        assert parse_qs(request.content.decode())['o:testmode'] == ['yes']
        return httpx.Response(200, json={'id': '<test@example.invalid>'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = briefings._send({'subject': 'Test', 'text': 'Test'}, report_env, 'key', test=True, client=client)
    assert result['state'] == 'test_accepted'
    assert result['inbox_confirmed'] is False


def test_world_readable_credentials_rejected(report_env):
    path = briefings.locations()['config'] / 'briefing-credentials.env'
    path.parent.mkdir(parents=True)
    path.write_text('MAILGUN_API_KEY=secret\n')
    path.chmod(0o644)
    with pytest.raises(briefings.BriefingError, match='unsafe_briefing_credential_permissions'):
        briefings._credentials()


@pytest.mark.parametrize('change', [
    {'monthly_usd': 'NaN'}, {'hosted_processing': 'yes'},
    {'remote_hosts': [{'name': '-oProxyCommand=bad', 'root': '/tmp'}]},
    {'roots': ['relative']}, {'conversation_harnesses': ['unknown']},
])
def test_invalid_config_cannot_expand_scope(report_env, tmp_path, change):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({**report_env, **change}))
    with pytest.raises((briefings.BriefingError, ValueError)):
        briefings.load_config(path)


def test_prepared_report_is_not_sent_after_scope_changes(report_env, monkeypatch):
    fake_generation(monkeypatch)
    briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24))
    report_env['roots'] = ['/different/approved/scope']
    monkeypatch.setattr(briefings, '_send', lambda *a, **kw: pytest.fail('old scope must not be delivered'))
    with pytest.raises(briefings.BriefingError, match='configuration_changed'):
        briefings.run(report_env, kind='daily', report_date=date(2026, 9, 24), send=True)
