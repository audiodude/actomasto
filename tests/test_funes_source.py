"""Real fork CLI against authored synthetic originals; never enroll user sources."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest

from actomasto.common import SourceCancelled
from actomasto.funes_source import FunesSource, SourceError

FIXTURES = Path(__file__).parent / 'fixtures'
FILES = {'claude': 'claude-2.1.263.jsonl', 'codex': 'codex-0.144.1.jsonl', 'omp': 'omp-session3.jsonl'}
START = 1767225600.0


class Source:
    def __init__(self, tmp_path, client, binary, fixture=None):
        self.client = client
        self.root = tmp_path / 'sessions'
        self.root.mkdir(parents=True)
        self.repo = tmp_path / 'public'
        self.repo.mkdir()
        self.path = self.root / 'session.jsonl'
        self.records = [json.loads(line.replace('/work/public', str(self.repo)))
                        for line in (FIXTURES / (fixture or FILES[client])).read_text().splitlines()]
        self.cursors = {}
        self.repositories = [{'id': 'github.com:1', 'paths': [str(self.repo)]}]
        scope = tmp_path / 'scope.json'
        scope.write_text(json.dumps({'version': 1, 'roots': {key: [str(self.root)] if key == client else [] for key in FILES}}))
        scope.chmod(0o600)
        self.config = {'executable': binary, 'corpus': str(tmp_path / 'corpus'), 'scope': str(scope)}
        self.api = FunesSource(self.config)
        self.write(self.records)

    def write(self, records, suffix=b''):
        self.path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in records) + suffix)

    def operator(self, op, **fields):
        request = {'protocol': 1, 'op': op, 'corpus': self.config['corpus'], 'scope': self.config['scope'], **fields}
        result = subprocess.run([self.config['executable'], 'source'], input=json.dumps(request),
                                capture_output=True, text=True, timeout=120)
        response = json.loads(result.stdout)
        assert response['ok'], response
        return response['result']

    def refresh(self):
        return self.operator('refresh')

    def stream(self, eligible=lambda repo, start, end: True, cancelled=None, seen=None):
        self.api = FunesSource(self.config, cancelled)
        return self.api.collect(self.client, self.repositories, self.cursors.get,
                                lambda key, value: self.cursors.__setitem__(key, copy.deepcopy(value)), eligible, seen)

    def read(self, eligible=lambda repo, start, end: True, cancelled=None, seen=None):
        self.refresh()
        return list(self.stream(eligible, cancelled, seen))


@pytest.mark.parametrize('client,fixture', [*FILES.items(), ('claude', 'claude-2.1.226.jsonl')])
def test_original_complete_turn_identity_and_provenance(tmp_path, funes_bin, client, fixture):
    source = Source(tmp_path, client, funes_bin, fixture)
    unit, = source.read()
    assert [item['text'] for item in unit['items']] == [
        'Explain the cache change.', 'I am checking cache invalidation.', 'I fixed cache invalidation.']
    assert [item['provenance'] for item in unit['items']] == ['user_reported', 'assistant_reported', 'assistant_reported']
    session = unit['source_ref']['session_id']
    user = unit['source_ref']['message_ids'][0]
    assert unit['id'] == hashlib.sha256(f'{client}:{session}:{user}'.encode()).hexdigest()
    assert unit['event_time'] == START and unit['event_end'] >= START + 3
    assert str(tmp_path) not in json.dumps(unit)
    assert 'cache' not in json.dumps(source.cursors)
    assert source.read() == []


@pytest.mark.parametrize('client', FILES)
def test_partial_write_restart_rotation_and_truncation(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    final = json.dumps(source.records[-1]).encode()
    source.write(source.records[:-1], final[:len(final)//2])
    assert source.read() == []
    assert source.api.status['streams']['incomplete_write'] == 1
    source.cursors = json.loads(json.dumps(source.cursors))
    source.write(source.records[:2])
    assert source.read() == []
    source.write(source.records)
    unit, = source.read()
    source.path.rename(source.root / 'rotated.jsonl')
    source.write(source.records)
    assert source.read() == []
    assert unit['items'][-1]['text'] == 'I fixed cache invalidation.'


@pytest.mark.parametrize('client', FILES)
def test_malformed_stream_does_not_hide_independent_source(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    source.write(source.records[:-1], b'{"type":bad}\n')
    assert source.read() == []
    assert source.api.status['streams']['malformed_record'] == 1
    (source.root / 'other.jsonl').write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in source.records))
    assert source.read()[0]['items'][-1]['text'] == 'I fixed cache invalidation.'


@pytest.mark.parametrize('client', FILES)
def test_unknown_schema_pauses_only_affected_harness_and_retries(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    rows = copy.deepcopy(source.records)
    message = rows[-2]['payload'] if client == 'codex' else rows[-1]['message']
    message['content'].append({'type': 'future_private_payload', 'text': 'DO_NOT_COLLECT'})
    source.write(rows)
    with pytest.raises(SourceError, match='unknown_content_schema'):
        source.read()
    assert 'DO_NOT_COLLECT' not in json.dumps(source.cursors)
    other = next(key for key in FILES if key != client)
    source.api.health(other)
    source.write(source.records)
    assert source.read()[0]['items'][0]['provenance'] == 'user_reported'


@pytest.mark.parametrize('client', FILES)
def test_excluded_whole_interval_remains_terminal(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    assert source.read(lambda repo, start, end: not (start < START + 2.5 and end >= START + 1.5)) == []
    assert source.read() == []
    assert any(value.get('reason') == 'ineligible_interval' for value in source.cursors.values())


@pytest.mark.parametrize('client', FILES)
def test_future_records_and_eof_are_not_completion(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    rows = copy.deepcopy(source.records)
    if client == 'codex':
        rows.pop()
    elif client == 'claude':
        rows[-1]['message']['stop_reason'] = 'max_tokens'
    else:
        rows[-1]['message']['stopReason'] = 'aborted'
    source.write(rows)
    assert source.read() == []
    assert source.api.status['pending_turns'] >= 1
    rows = copy.deepcopy(source.records)
    if client == 'omp':
        rows[-1]['message']['completedAt'] = (time.time() + 3600) * 1000
    else:
        rows[-1]['timestamp'] = '2999-01-01T00:00:00Z'
    source.write(rows)
    assert source.read() == []
    assert source.api.status['streams']['deferred_future'] == 1
    source.write(source.records)
    assert source.read()[0]['event_end'] < time.time()


@pytest.mark.parametrize('client', ['claude', 'omp'])
def test_shared_ancestor_branch_does_not_replay_previous_turn(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    first, = source.read()
    user = copy.deepcopy(next(row for row in source.records if row.get('type') == 'user' and row.get('origin')
                              or row.get('message', {}).get('attribution') == 'user'))
    final = copy.deepcopy(source.records[-1])
    if client == 'claude':
        user.update(uuid='u2', parentUuid='u1', timestamp='2026-01-01T00:00:05Z')
        user['message']['content'] = 'Explain the branch change.'
        final.update(uuid='a3', parentUuid='u2', timestamp='2026-01-01T00:00:06Z')
    else:
        user.update(id='u2', parentId='u1', timestamp='2026-01-01T00:00:05Z')
        user['message'].update(timestamp=1767225605000, content=[{'type': 'text', 'text': 'Explain the branch change.'}])
        final.update(id='a3', parentId='u2', timestamp='2026-01-01T00:00:06Z')
        final['message'].update(timestamp=1767225606000, completedAt=1767225606500)
    source.write(source.records + [user, final])
    second, = source.read()
    assert second['id'] != first['id']
    assert [item['text'] for item in second['items']] == ['Explain the branch change.', 'I fixed cache invalidation.']


@pytest.mark.parametrize('client', FILES)
def test_untrusted_human_provenance_never_emits(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    if client == 'codex':
        source.records = [row for row in source.records if row.get('payload', {}).get('type') != 'user_message']
    elif client == 'omp':
        for row in source.records:
            if row.get('message', {}).get('role') == 'user':
                row['message']['attribution'] = 'agent'
    else:
        source.records[0].update(origin={'kind': 'task-notification'}, promptSource='typed')
    source.write(source.records)
    assert source.read() == []


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_excluded_record_project_boundary_rejects_whole_turn(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    if client == 'claude':
        source.records[-1]['cwd'] = str(tmp_path)
    else:
        source.records.insert(-1, {'type': 'turn_context', 'timestamp': '2026-01-01T00:00:03.5Z',
                                   'payload': {'cwd': str(tmp_path), 'turn_id': 'turn1'}})
    source.write(source.records)
    assert source.read() == []


@pytest.mark.parametrize('client', FILES)
def test_ineligible_nested_repository_cannot_fall_back_to_parent(tmp_path, funes_bin, client):
    source = Source(tmp_path, client, funes_bin)
    source.repositories = [{'id': 'public-parent', 'paths': [str(tmp_path)]},
                           {'id': 'blocked-child', 'paths': [str(source.repo)]}]
    assert source.read(lambda repo, start, end: repo == 'public-parent') == []
    assert source.read() == []


@pytest.mark.parametrize('allowed', [True, False])
def test_cancel_before_content_or_exclusion_leaves_retryable(tmp_path, funes_bin, allowed):
    source = Source(tmp_path, 'omp', funes_bin)
    stopped = False
    def eligible(*args):
        nonlocal stopped
        stopped = True
        return allowed
    def cancelled():
        if stopped:
            raise SourceCancelled()
    with pytest.raises(SourceCancelled):
        source.read(eligible, cancelled)
    assert source.cursors == {}
    assert source.read()[0]['items'][0]['text'] == 'Explain the cache change.'


def test_cancel_after_yield_retains_original_unit_identity(tmp_path, funes_bin):
    source = Source(tmp_path, 'omp', funes_bin)
    stopped = False
    def cancelled():
        if stopped:
            raise SourceCancelled()
    source.refresh()
    stream = source.stream(cancelled=cancelled)
    unit = next(stream)
    stopped = True
    with pytest.raises(SourceCancelled):
        next(stream)
    assert source.cursors == {}
    assert source.read() == [unit]


def test_missing_time_excludes_turn_and_terminal_marker_survives(tmp_path, funes_bin):
    source = Source(tmp_path, 'claude', funes_bin)
    source.records[2]['timestamp'] = 'not-a-timestamp'
    source.write(source.records)
    assert source.read() == []
    assert any(value.get('reason') == 'ambiguous_turn' for value in source.cursors.values())


@pytest.mark.parametrize('flags', [{'isVisibleInTranscriptOnly': True}, {'isAbortedMidStream': True},
                                   {'supersedesUuids': ['a1']}, {'interruptedMessageId': 'a1'}, {'agentId': 'agent'}])
def test_legacy_exclusion_does_not_poison_next_turn(tmp_path, funes_bin, flags):
    source = Source(tmp_path, 'claude', funes_bin, 'claude-2.1.226.jsonl')
    source.records[-1].update(flags)
    user = copy.deepcopy(source.records[1])
    user.update(uuid='u2', parentUuid='a2', timestamp='2026-01-01T00:00:04Z')
    user['message']['content'] = 'Explain the next change.'
    final = copy.deepcopy(source.records[-1])
    for key in flags:
        final.pop(key)
    final.update(uuid='a3', parentUuid='u2', timestamp='2026-01-01T00:00:05Z')
    source.write(source.records + [user, final])
    assert [[item['text'] for item in unit['items']] for unit in source.read()] == [
        ['Explain the next change.', 'I fixed cache invalidation.']]


def test_revision_bound_reads_and_missing_originals(tmp_path, funes_bin):
    source = Source(tmp_path, 'omp', funes_bin)
    source.refresh()
    original = source.api.request('enumerate', harness='omp')['sources'][0]
    source.write(source.records[:-1])
    with pytest.raises(SourceError, match='source_changed'):
        source.api.request('read', source_id=original['id'], revision=original['revision'], ordinal=0)
    source.path.unlink()
    with pytest.raises(SourceError, match='source_missing'):
        source.api.request('read', source_id=original['id'], revision=original['revision'], ordinal=0)


def test_snapshot_pages_late_backfill_and_cursor_recovery(tmp_path, funes_bin):
    source = Source(tmp_path, 'omp', funes_bin)
    (source.root / 'z.jsonl').write_bytes(source.path.read_bytes())
    source.refresh()
    page = source.api.request('enumerate', harness='omp', limit=1)
    assert page['next_cursor']
    (source.root / 'a.jsonl').write_bytes(source.path.read_bytes())
    source.refresh()
    next_page = source.api.request('enumerate', harness='omp', limit=1, cursor=page['next_cursor'])
    assert next_page['snapshot'] == page['snapshot']
    assert next_page['sources'][0]['id'] != page['sources'][0]['id']
    source.cursors['funes-enumeration:omp'] = {'cursor': 'invalid-before-rebuild'}
    assert source.read()[0]['items'][0]['text'] == 'Explain the cache change.'
    assert source.read() == []


def test_remote_scope_and_metadata_content_are_rejected(tmp_path, funes_bin):
    source = Source(tmp_path, 'omp', funes_bin)
    request = {'protocol': 1, 'op': 'enumerate', 'corpus': 'hf://private/memory', 'scope': source.config['scope']}
    process = subprocess.run([funes_bin, 'source'], input=json.dumps(request), text=True, capture_output=True)
    assert process.returncode != 0 and json.loads(process.stdout)['ok'] is False
    source.refresh()
    page = source.api.request('enumerate')
    descriptor = page['sources'][0]
    metadata = source.api.request('turns', source_id=descriptor['id'], revision=descriptor['revision'])
    assert 'Explain the cache' not in json.dumps([page, metadata])
    assert 'I fixed cache' not in process.stderr


def test_legacy_adapter_only_exclusion_and_seen_skip_original_read(tmp_path, funes_bin, monkeypatch):
    source = Source(tmp_path, 'omp', funes_bin)
    source.refresh()
    first, = source.read()
    source.cursors = {'adapter-unit:omp:' + first['id']: {'reason': 'oversized_source'}}
    assert source.read() == []
    source.cursors = {}
    request = FunesSource.request
    def no_originals(self, op, **fields):
        if op == 'read':
            pytest.fail('durably consumed units must not read original content')
        return request(self, op, **fields)
    monkeypatch.setattr(FunesSource, 'request', no_originals)
    assert source.read(seen=lambda unit: unit['id'] == first['id']) == []


@pytest.mark.parametrize('tag', ['ſyſtem-reminder', 'system-dırectıve', 'tasK-notification'])
def test_unicode_casefolded_injection_frames_keep_legacy_exclusions(tmp_path, funes_bin, tag):
    source = Source(tmp_path, 'omp', funes_bin)
    user = next(row['message'] for row in source.records if row.get('message', {}).get('role') == 'user')
    user['content'][0]['text'] = f'Explain the cache change. <{tag}>PRIVATE_INJECTION</{tag}>'
    source.write(source.records)
    unit, = source.read()
    assert unit['items'][0]['text'] == 'Explain the cache change.'
    assert 'PRIVATE_INJECTION' not in json.dumps(unit)


def test_stale_independent_index_is_visible_and_retryable(tmp_path, funes_bin):
    import sqlite3
    source = Source(tmp_path, 'omp', funes_bin)
    source.refresh()
    with sqlite3.connect(Path(source.config['corpus']) / 'local-source/inventory.sqlite3') as db:
        db.execute('UPDATE snapshots SET refreshed_at=0')
    with pytest.raises(SourceError, match='coverage_unavailable'):
        list(source.stream())
    assert source.api.status['coverage'] == 'lagging'
    assert source.api.status['lag_seconds'] > 300
    assert source.cursors == {}
    assert source.read()[0]['items'][0]['text'] == 'Explain the cache change.'


def test_raw_fallback_inputs_preserve_original_byte_identity(tmp_path, funes_bin):
    source = Source(tmp_path, 'codex', funes_bin)
    source.refresh()
    descriptor = source.api.request('enumerate', harness='codex')['sources'][0]
    turn = source.api.request('turns', source_id=descriptor['id'], revision=descriptor['revision'])['turns'][0]
    identity = turn['items'][0]['identity']
    raw = source.path.read_bytes().splitlines(keepends=True)[5]
    raw_digest = hashlib.sha256(raw.hex().encode()).hexdigest()
    assert identity == {'native_id': None, 'record_index': 5, 'turn_context': 'turn1', 'raw_record_digest': raw_digest}
    message = hashlib.sha256(f'sanitized-codex:turn1:5:{raw_digest}'.encode()).hexdigest()
    assert turn['user_id'] == message
    assert source.read()[0]['id'] == hashlib.sha256(f'codex:sanitized-codex:{message}'.encode()).hexdigest()


def test_missing_enrolled_root_is_unavailable_not_empty_activity(tmp_path, funes_bin):
    source = Source(tmp_path, 'omp', funes_bin)
    source.path.unlink()
    source.root.rmdir()
    with pytest.raises(SourceError, match='coverage_unavailable'):
        source.read()
    assert source.api.status['coverage'] == 'unavailable'
    assert source.cursors == {}
    source.root.mkdir()
    source.write(source.records)
    assert source.read()[0]['items'][0]['text'] == 'Explain the cache change.'
