from __future__ import annotations

import copy
import json
import os
import tempfile
import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import secure_dir

CONSENT = ('Enabling automatically includes newly discovered verified-public repositories under your roots. '
           'Each receives a seven-day import; unpushed commits qualify. Eligible AI conversations need not '
           'be public. Committed content and conversations are sent to Anthropic. Filtering cannot guarantee '
           'confidentiality. Configure blocklists before enabling. No posts are published.')
DEFAULTS = {
    'version': 1,
    'discovery': {'roots': []},
    'identity': {'author_emails': []},
    'blocklist': {'repositories': [], 'paths': [], 'text': [], 'scoped': []},
    'sources': {'claude_root': '~/.claude/projects', 'codex_root': '~/.codex/sessions', 'omp_root': '~/.omp/agent/sessions'},
    'generation': {'model': 'claude-haiku-4-5-20251001', 'character_limit': 500, 'interval_minutes': 30},
    'budget': {'monthly_usd': '20.00', 'timezone': 'UTC'},
    'notifications': {'enabled': True},
}


class ConfigError(ValueError):
    pass


def _strings(value, key, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ConfigError(f'invalid_{key}')
    return value


def validate(value):
    if not isinstance(value, dict):
        raise ConfigError('invalid_configuration')
    result = copy.deepcopy(DEFAULTS)
    for section, values in value.items():
        if section not in DEFAULTS:
            raise ConfigError('unknown_configuration_key')
        if section == 'version':
            if type(values) is not int or values != 1:
                raise ConfigError('unsupported_configuration_version')
            continue
        if not isinstance(values, dict) or set(values) - set(DEFAULTS[section]):
            raise ConfigError(f'invalid_{section}_keys')
        result[section].update(values)
    roots = _strings(result['discovery']['roots'], 'roots', True)
    for root in roots:
        if not Path(root).is_absolute():
            raise ConfigError('roots_must_be_absolute')
    result['discovery']['roots'] = sorted(set(str(Path(r).resolve()) for r in roots))
    emails = _strings(result['identity']['author_emails'], 'author_emails', True)
    if any('@' not in email.strip() or '\n' in email for email in emails):
        raise ConfigError('invalid_author_emails')
    result['identity']['author_emails'] = sorted(set(e.strip().casefold() for e in emails))
    for key in ('repositories', 'paths', 'text'):
        _strings(result['blocklist'][key], f'blocklist_{key}')
    scoped = result['blocklist']['scoped']
    if not isinstance(scoped, list):
        raise ConfigError('invalid_scoped_rules')
    for rule in scoped:
        if not isinstance(rule, dict) or set(rule) - {'repository', 'paths', 'text'} or not isinstance(rule.get('repository'), str) or not rule['repository'].strip():
            raise ConfigError('invalid_scoped_rule')
        for key in ('paths', 'text'):
            _strings(rule.get(key, []), f'scoped_{key}')
    for root in result['sources'].values():
        if not isinstance(root, str) or not Path(root).expanduser().is_absolute():
            raise ConfigError('invalid_source_root')
    generation = result['generation']
    if not isinstance(generation['model'], str) or not generation['model'].strip():
        raise ConfigError('invalid_model')
    for key, maximum in [('character_limit', 5000), ('interval_minutes', None)]:
        n = generation[key]
        if type(n) is not int or n < 1 or (maximum and n > maximum):
            raise ConfigError(f'invalid_{key}')
    amount = result['budget']['monthly_usd']
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        raise ConfigError('invalid_budget')
    try:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0:
            raise ConfigError('invalid_budget')
    except InvalidOperation:
        raise ConfigError('invalid_budget') from None
    result['budget']['monthly_usd'] = str(amount)
    try:
        ZoneInfo(result['budget']['timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ConfigError('invalid_timezone') from None
    if type(result['notifications']['enabled']) is not bool:
        raise ConfigError('invalid_notifications_enabled')
    return result


def load(path):
    try:
        with Path(path).open('rb') as stream:
            return validate(tomllib.load(stream))
    except tomllib.TOMLDecodeError:
        raise ConfigError('invalid_toml') from None


def local_timezone():
    try:
        target = str(Path('/etc/localtime').resolve())
        zone = target.split('/zoneinfo/', 1)[1]
        ZoneInfo(zone)
        return zone
    except (IndexError, ValueError, ZoneInfoNotFoundError):
        return 'UTC'


def save(path, config):
    config = validate(config)
    lines = ['version = 1', '']
    for section, values in config.items():
        if section == 'version':
            continue
        lines.append(f'[{section}]')
        for key, value in values.items():
            if key == 'scoped':
                continue
            lines.append(f'{key} = {json.dumps(value, ensure_ascii=False)}')
        lines.append('')
    for rule in config['blocklist']['scoped']:
        lines.append('[[blocklist.scoped]]')
        for key, value in rule.items():
            lines.append(f'{key} = {json.dumps(value, ensure_ascii=False)}')
        lines.append('')
    directory = secure_dir(Path(path).parent)
    fd, name = tempfile.mkstemp(dir=directory, prefix='.config-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def preview(config):
    """Explicit validation reads metadata only; no enrollment or network calls."""
    from .discovery import discover
    from .git_source import GitSourceError, _git
    from .policy import Policy, sensitive_path
    policy = Policy(config)
    matches = []
    for entry in discover(config['discovery']['roots']):
        origin = entry.get('origin')
        row = {'repository': origin, 'reason': entry.get('reason'), 'blocked': False,
               'matched_paths': [], 'sensitive_path_count': 0}
        if origin:
            row['blocked'] = policy.repository_blocked(origin)
            try:
                refs = _git(entry['path'], 'for-each-ref', '--format=%(objectname)', 'refs/heads', 'refs/remotes', 'refs/tags').decode().splitlines()
                names = set()
                for ref in set(refs):
                    for name in _git(entry['path'], 'ls-tree', '-rz', '--name-only', ref).decode('utf-8', 'replace').split('\0'):
                        if name:
                            names.add(name)
                        if len(names) > 100000:
                            raise GitSourceError('preview_oversized')
                for name in sorted(names):
                    if policy.path_blocked(name, origin):
                        if any(sensitive_path(part) for part in name.split('/')):
                            row['sensitive_path_count'] += 1
                        else:
                            row['matched_paths'].append(name)
            except GitSourceError:
                row['reason'] = 'metadata_preview_incomplete'
        matches.append(row)
    return {'valid': True, 'roots': config['discovery']['roots'], 'matches': matches,
            'literal_rule_count': len(config['blocklist']['text']) +
                sum(len(rule.get('text', [])) for rule in config['blocklist']['scoped']),
            'literal_preview': 'source_content_not_inspected'}
