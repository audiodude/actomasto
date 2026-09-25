"""Standalone personal reports: independent consent, budget, archive and delivery.

This module never opens the draft collector's Store or changes its controls.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from zoneinfo import ZoneInfo

import httpx

from .common import credential, locations, secure_dir, terminal_safe, writer_lock
from .policy import Policy, PolicyError

RESERVATION = 100_000  # micro-USD; bounded input + output at the pinned model rates
CONFIG_KEYS = {'version', 'roots', 'author_emails', 'timezone', 'remote_hosts',
               'inventory_db', 'blocklist', 'funes',
               'conversation_harnesses', 'hosted_processing', 'monthly_usd', 'mailgun'}


class BriefingError(RuntimeError):
    pass


def _private_json(path, value):
    secure_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.briefing-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def config_path():
    return locations()['config'] / 'briefings.json'


def load_config(path=None):
    path = Path(path) if path else config_path()
    with path.open() as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or set(value) - CONFIG_KEYS or type(value.get('version')) is not int or value['version'] != 1:
        raise BriefingError('invalid_briefing_config')
    for key in ('roots', 'author_emails'):
        if not isinstance(value.get(key), list) or not value[key] or any(
                not isinstance(item, str) or not item or '\x00' in item for item in value[key]):
            raise BriefingError('invalid_briefing_scope')
    if any(not Path(root).is_absolute() for root in value['roots']):
        raise BriefingError('absolute_roots_required')
    if any(not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+', item) for item in value['author_emails']):
        raise BriefingError('invalid_author_email')
    try:
        ZoneInfo(value['timezone'])
    except (KeyError, TypeError, ValueError):
        raise BriefingError('invalid_briefing_timezone') from None
    if type(value.get('hosted_processing')) is not bool:
        raise BriefingError('explicit_hosted_consent_required')
    try:
        amount = Decimal(str(value.get('monthly_usd', '5.00')))
        if not amount.is_finite() or amount < 0 or amount > 1000:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise BriefingError('invalid_briefing_budget') from None
    value['monthly_usd'] = str(amount)
    hosts = value.get('remote_hosts', [])
    if not isinstance(hosts, list):
        raise BriefingError('invalid_remote_hosts')
    for host in hosts:
        if (not isinstance(host, dict) or set(host) != {'name', 'root'}
                or not isinstance(host['name'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', host['name'])
                or not isinstance(host['root'], str) or not host['root'].startswith(('/', '~/'))
                or any(c in host['root'] for c in '\x00\r\n')):
            raise BriefingError('invalid_remote_host')
    harnesses = value.get('conversation_harnesses', [])
    if not isinstance(harnesses, list) or any(h not in ('claude', 'codex', 'omp') for h in harnesses):
        raise BriefingError('invalid_conversation_harnesses')
    if harnesses:
        from .funes_source import FunesSource
        FunesSource(value.get('funes'))  # configuration only; no reads
    inventory = value.get('inventory_db')
    if inventory and (not isinstance(inventory, str) or not Path(inventory).is_absolute()):
        raise BriefingError('absolute_database_path_required')
    try:
        Policy(value)
    except (PolicyError, TypeError, AttributeError):
        raise BriefingError('invalid_briefing_blocklist') from None
    mail = value.get('mailgun', {})
    if (not isinstance(mail, dict) or set(mail) - {'domain', 'sender', 'recipient', 'region'}):
        raise BriefingError('invalid_mailgun_config')
    if mail:
        if (set(mail) != {'domain', 'sender', 'recipient', 'region'}
                or mail['region'] not in ('US', 'EU')
                or not isinstance(mail['domain'], str)
                or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', mail['domain'])
                or any(not isinstance(mail[k], str) or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+', mail[k])
                       for k in ('sender', 'recipient'))):
            raise BriefingError('invalid_mailgun_config')
    return value


def _credentials():
    result = {key: os.environ[key] for key in ('ANTHROPIC_API_KEY', 'MAILGUN_API_KEY') if os.environ.get(key)}
    path = locations()['config'] / 'briefing-credentials.env'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    if fd is not None:
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BriefingError('unsafe_briefing_credential_permissions')
            for line in stream:
                name, separator, value = line.strip().partition('=')
                if separator and name in ('ANTHROPIC_API_KEY', 'MAILGUN_API_KEY'):
                    result.setdefault(name, value.strip().strip('\"\''))
    if not result.get('ANTHROPIC_API_KEY'):
        result['ANTHROPIC_API_KEY'] = credential()
    return result


def _root():
    return secure_dir(locations()['data'] / 'briefings')


def _read_state(root):
    path = root / 'state.json'
    return json.loads(path.read_text()) if path.exists() else {'reports': {}, 'charges': {}}


def _key(kind, report_date, project):
    suffix = '-' + hashlib.sha256(project.encode()).hexdigest()[:12] if project else ''
    return f'{kind}-{report_date.isoformat()}{suffix}'


def _send(record, config, api_key, *, test=False, client=None):
    mail = config.get('mailgun')
    if not mail or not api_key or api_key.startswith('oc-sent-'):
        raise BriefingError('mailgun_credential_or_config_missing')
    host = 'api.mailgun.net' if mail['region'] == 'US' else 'api.eu.mailgun.net'
    fields = {'from': mail['sender'], 'to': mail['recipient'], 'subject': record['subject'], 'text': record['text']}
    if test:
        fields['o:testmode'] = 'yes'
    owned = client is None
    http = client or httpx.Client(trust_env=False)
    try:
        with http.stream('POST', f'https://{host}/v3/{mail["domain"]}/messages',
                         auth=('api', api_key), data=fields, timeout=30, follow_redirects=False) as response:
            if response.status_code != 200:
                return {'state': 'rejected', 'http_status': response.status_code}
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 65536:
                    return {'state': 'unknown'}
            result = json.loads(payload)
            if not isinstance(result, dict) or not isinstance(result.get('id'), str):
                return {'state': 'unknown'}
            return {'state': 'test_accepted' if test else 'accepted', 'message_id': result['id'],
                    'inbox_confirmed': False}
    except (httpx.HTTPError, ValueError):
        return {'state': 'unknown'}
    finally:
        if owned:
            http.close()


def run(config, *, kind, report_date=None, project=None, dry_run=False, send=False):
    from .briefing_sources import collect
    from .briefing_generation import generate, prepare
    now = datetime.now(ZoneInfo(config['timezone']))
    report_date = report_date or now.date()
    if (kind == 'reentry') != bool(project):
        raise BriefingError('reentry_requires_project_only')
    if dry_run and send:
        raise BriefingError('dry_run_cannot_send')
    if dry_run:
        bundle = collect(config, now=now, project=project)
        return prepare(bundle, kind=kind, report_date=report_date, timezone=config['timezone'], project=project)
    if not config['hosted_processing']:
        raise BriefingError('hosted_processing_not_authorized')
    secrets = _credentials()
    if not secrets.get('ANTHROPIC_API_KEY'):
        raise BriefingError('anthropic_credential_missing')
    if send and (not config.get('mailgun') or not secrets.get('MAILGUN_API_KEY')):
        raise BriefingError('mailgun_credential_or_config_missing')
    root = _root()
    key = _key(kind, report_date, project)
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    with writer_lock(root):
        state = _read_state(root)
        record = state['reports'].get(key)
        if record is not None and record.get('config_fingerprint') != fingerprint:
            raise BriefingError('prepared_report_configuration_changed')
        if record is None:
            month = now.strftime('%Y-%m')
            spent = sum(item['micro_usd'] for item in state['charges'].values() if item['month'] == month)
            if spent + RESERVATION > int(Decimal(config['monthly_usd']) * 1_000_000):
                raise BriefingError('briefing_monthly_budget_exhausted')
            if key in state['charges']:
                raise BriefingError('generation_already_attempted_inspect_status')
            bundle = collect(config, now=now, project=project)
            # Durably reserve before provider dispatch. Ambiguous attempts are not retried.
            state['charges'][key] = {'month': month, 'micro_usd': RESERVATION, 'estimated': True}
            _private_json(root / 'state.json', state)
            result = generate(bundle, kind=kind, report_date=report_date, timezone=config['timezone'],
                              api_key=secrets['ANTHROPIC_API_KEY'], project=project)
            usage = result.get('usage', {})
            if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')):
                from .generation import MODEL_RATES
                rates = MODEL_RATES[result['model']]
                cost = usage['input_tokens'] * rates[0] + usage['output_tokens'] * rates[1]
                if cost <= RESERVATION and not usage.get('cache_creation_input_tokens') and not usage.get('cache_read_input_tokens'):
                    state['charges'][key].update(micro_usd=cost, estimated=False)
            record = {**result, 'id': key, 'kind': kind, 'date': report_date.isoformat(),
                      'created_at': now.isoformat(), 'project': project, 'delivery': {'state': 'not_sent'},
                      'coverage': result.get('coverage', bundle.get('coverage', [])),
                      'config_fingerprint': fingerprint}
            state['reports'][key] = record
            _private_json(root / 'state.json', state)
            _private_json(root / (key + '.json'), record)
        if send and record['delivery']['state'] == 'not_sent':
            # Persist sending first: a crash after acceptance must not duplicate mail.
            record['delivery'] = {'state': 'sending', 'recipient': config['mailgun']['recipient']}
            _private_json(root / 'state.json', state)
            record['delivery'] = _send(record, config, secrets.get('MAILGUN_API_KEY'))
            _private_json(root / 'state.json', state)
            _private_json(root / (key + '.json'), record)
        return record


def service(config, action):
    paths = locations()
    kinds = ('daily', 'weekly')
    if action == 'install':
        if not config['hosted_processing'] or not config.get('mailgun'):
            raise BriefingError('scheduled_hosted_delivery_not_configured')
        keys = _credentials()
        if not all(keys.get(k) for k in ('ANTHROPIC_API_KEY', 'MAILGUN_API_KEY')):
            raise BriefingError('scheduled_credentials_missing')
        executable = shutil.which('actomasto')
        if not executable:
            raise BriefingError('absolute_cli_executable_unavailable')
        def quote(text):
            return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'
        paths['systemd'].mkdir(parents=True, exist_ok=True)
        for kind in kinds:
            calendar = f'{"Mon " if kind == "weekly" else ""}*-*-* 06:{"15" if kind == "weekly" else "00"}:00 {config["timezone"]}'
            unit = ('[Unit]\nDescription=Actomasto personal ' + kind + ' briefing\n\n[Service]\nType=oneshot\n'
                    + f'ExecStart={quote(Path(executable).absolute())} briefing run {kind} --send\n'
                    + f'Environment={quote("XDG_CONFIG_HOME=" + str(paths["config"].parent))} '
                    + f'{quote("XDG_DATA_HOME=" + str(paths["data"].parent))}\n'
                    + 'UMask=0077\nNoNewPrivileges=yes\nTimeoutStartSec=15min\n')
            timer = ('[Unit]\nDescription=Schedule Actomasto ' + kind + ' briefing\n\n[Timer]\n'
                     + f'OnCalendar={calendar}\nPersistent=false\n\n[Install]\nWantedBy=timers.target\n')
            for extension, content in (('service', unit), ('timer', timer)):
                target = paths['systemd'] / f'actomasto-briefing-{kind}.{extension}'
                target.write_text(content)
                target.chmod(0o600)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now',
                        *[f'actomasto-briefing-{kind}.timer' for kind in kinds]], check=True, capture_output=True)
    else:
        subprocess.run(['systemctl', '--user', 'disable', '--now',
                        *[f'actomasto-briefing-{kind}.timer' for kind in kinds]], check=True, capture_output=True)
    return {'service': action, 'daily': '06:00', 'weekly': 'Monday 06:15',
            'timezone': config['timezone'], 'collector_controls_changed': False}


def add_parser(commands):
    command = commands.add_parser('briefing', help='Independent personal reports; never enables draft collection')
    subs = command.add_subparsers(dest='briefing_action', required=True)
    run_parser = subs.add_parser('run')
    run_parser.add_argument('kind', choices=('daily', 'weekly', 'reentry'))
    run_parser.add_argument('--date', type=date.fromisoformat)
    run_parser.add_argument('--project')
    run_parser.add_argument('--dry-run', action='store_true', help='Local filtered evidence only; no model or email')
    run_parser.add_argument('--send', action='store_true')
    run_parser.add_argument('--json', action='store_true')
    subs.add_parser('status')
    subs.add_parser('validate')
    show = subs.add_parser('show')
    show.add_argument('id')
    svc = subs.add_parser('service')
    svc.add_argument('service_action', choices=('install', 'disable'))
    subs.add_parser('test-delivery', help='Mailgun test mode: API acceptance only, no inbox delivery')


def execute(args):
    config = load_config()
    action = args.briefing_action
    if action == 'run':
        from .briefing_sources import BriefingSourceError
        from .briefing_generation import BriefingGenerationError
        try:
            result = run(config, kind=args.kind, report_date=args.date, project=args.project,
                         dry_run=args.dry_run, send=args.send)
        except (BriefingSourceError, BriefingGenerationError) as error:
            raise BriefingError(str(error)) from None
        if not args.json and not args.dry_run:
            print(terminal_safe(result['text']))
            print('\nDelivery: ' + result['delivery']['state'])
        else:
            print(json.dumps(terminal_safe(result), ensure_ascii=False, indent=2))
        return 0 if not args.send or result.get('delivery', {}).get('state') == 'accepted' else 1
    if action == 'validate':
        result = {'valid': True, 'hosted_processing': config['hosted_processing'],
                  'roots': config['roots'], 'remote_hosts': config.get('remote_hosts', []),
                  'conversation_harnesses': config.get('conversation_harnesses', []),
                  'monthly_usd': config['monthly_usd'], 'mailgun': config.get('mailgun', {})}
    elif action == 'service':
        result = service(config, args.service_action)
    elif action == 'test-delivery':
        result = _send({'subject': 'Actomasto transport verification (test mode)',
                        'text': 'Mailgun API test-mode verification. Do not deliver.'}, config,
                       _credentials().get('MAILGUN_API_KEY'), test=True)
    else:
        root = _root()
        with writer_lock(root):
            state = _read_state(root)
        if action == 'show':
            if args.id not in state['reports']:
                raise BriefingError('briefing_not_found')
            result = state['reports'][args.id]
        else:
            result = {'reports': [{k: r[k] for k in ('id', 'date', 'kind', 'delivery')}
                                  for r in state['reports'].values()], 'charges': state['charges'],
                      'collector_controls_changed': False}
    print(json.dumps(terminal_safe(result), ensure_ascii=False, indent=2))
    return 0 if action != 'test-delivery' or result['state'] == 'test_accepted' else 1
