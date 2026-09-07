from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import control, locations, secure_dir, terminal_safe, timestamp, writer_lock
from .config import CONSENT, ConfigError, DEFAULTS, load, local_timezone, save, validate
from .store import Store


def parser():
    p = argparse.ArgumentParser(prog='actomasto')
    commands = p.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--root', action='append', required=True)
    init.add_argument('--author-email', action='append', required=True)
    init.add_argument('--timezone', default=local_timezone())
    init.add_argument('--accept-hosted-processing', action='store_true')
    config = commands.add_parser('config').add_subparsers(dest='action', required=True)
    config.add_parser('validate')
    apply = config.add_parser('apply')
    apply.add_argument('--yes', action='store_true')
    service = commands.add_parser('service').add_subparsers(dest='action', required=True)
    service.add_parser('install')
    service.add_parser('uninstall')
    commands.add_parser('daemon')
    on = commands.add_parser('on')
    on.add_argument('--accept-hosted-processing', action='store_true')
    commands.add_parser('off')
    for command in ('status', 'repos', 'list', 'show'):
        item = commands.add_parser(command)
        item.add_argument('--json', action='store_true')
        if command == 'list':
            item.add_argument('--repo')
            item.add_argument('--since', type=timestamp)
            item.add_argument('--limit', type=int, default=20)
        if command == 'show':
            item.add_argument('id')
            item.add_argument('--evidence', action='store_true')
    purge = commands.add_parser('purge')
    group = purge.add_mutually_exclusive_group(required=True)
    group.add_argument('--repo')
    group.add_argument('--all', action='store_true')
    purge.add_argument('--yes', action='store_true')
    return p


def confirm(message, accepted=False):
    print(terminal_safe(message), file=sys.stderr)
    if accepted:
        return
    if not sys.stdin.isatty() or input('Type yes to confirm: ').strip().lower() != 'yes':
        raise ConfigError('confirmation_required')


def execute(command, **kwargs):
    try:
        return control(command, **kwargs)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    paths = locations()
    with writer_lock(paths['data']):
        store = Store(paths['data'])
        try:
            store.recover(time.time())
            from .daemon import Runtime
            result = Runtime(store).command({'command': command, **kwargs})
            if command == 'status':
                result['process_running'] = False
            return result
        finally:
            store.close()


def service(action):
    paths = locations()
    unit_path = paths['systemd'] / 'actomasto.service'
    if action == 'install':
        executable = shutil.which('actomasto')
        if executable is None:
            raise RuntimeError('absolute_cli_executable_unavailable')
        executable = str(Path(executable).absolute())
        # systemd specifier and quoting escaping; no shell execution.
        quote = lambda text: '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'
        unit = ('[Unit]\nDescription=Actomasto draft collector\n\n[Service]\nType=simple\n'
                f'ExecStart={quote(executable)} daemon\n'
                f'Environment={quote("XDG_CONFIG_HOME=" + str(paths["config"].parent))} '
                f'{quote("XDG_DATA_HOME=" + str(paths["data"].parent))} '
                f'{quote("XDG_RUNTIME_DIR=" + str(paths["runtime"].parent))}\n'
                'UMask=0077\nRestart=on-failure\nRestartSec=5\nNoNewPrivileges=yes\n\n[Install]\nWantedBy=default.target\n')
        paths['systemd'].mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit)
        unit_path.chmod(0o600)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'actomasto.service'], check=True, capture_output=True)
    else:
        execute('off')
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'actomasto.service'], check=True, capture_output=True)
        unit_path.unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
    return {'service': action, 'data_preserved': True}


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        paths = locations()
        if args.command == 'daemon':
            from .daemon import run
            run()
            return 0
        if args.command == 'init':
            path = paths['config'] / 'config.toml'
            if path.exists():
                raise ConfigError('configuration_already_exists')
            config = validate({'discovery': {'roots': args.root}, 'identity': {'author_emails': args.author_email},
                               'budget': {'timezone': args.timezone}})
            confirm(CONSENT, args.accept_hosted_processing)
            with writer_lock(paths['data']):
                store = Store(paths['data'])
                try:
                    if store.settings()['enabled']:
                        raise ConfigError('initialization_requires_disabled_state')
                    store.apply_config(config, time.time())
                    save(path, config)
                finally:
                    store.close()
            result = {'initialized': True, 'enabled': False}
        elif args.command == 'config':
            config = load(paths['config'] / 'config.toml')
            if args.action == 'validate':
                from .config import preview
                result = preview(config)
            else:
                confirm('Apply configuration and hosted-processing scope:\n' + '\n'.join(config['discovery']['roots']) + '\n' + CONSENT, args.yes)
                result = execute('apply', config=config)
        elif args.command == 'service':
            result = service(args.action)
        elif args.command == 'on':
            confirm(CONSENT, args.accept_hosted_processing)
            result = execute('on')
        elif args.command == 'off':
            result = execute('off')
        elif args.command in ('status', 'repos'):
            result = execute(args.command)
        elif args.command == 'list':
            if args.limit < 1:
                raise ConfigError('limit_must_be_positive')
            result = execute('list', repo=args.repo, since=args.since, limit=args.limit)
        elif args.command == 'show':
            result = execute('show', id=args.id, evidence=args.evidence)
            if result is None:
                print(json.dumps({'error': 'suggestion_not_found'}))
                return 3
        else:
            preview = execute('list', repo=args.repo, limit=1000000)
            confirm(f'Purge {len(preview)} drafts plus matching pending content; preserve spending and processing history. '
                    'Original sources, backups and provider-held requests are not erased.', args.yes)
            result = execute('purge', repo=args.repo)
        print(json.dumps(result if getattr(args, 'json', False) else terminal_safe(result), ensure_ascii=False,
                         indent=None if getattr(args, 'json', False) else 2))
        return 0
    except (ConfigError, ValueError):
        print(json.dumps({'error': 'invalid_configuration_or_arguments'}), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({'error': 'operational_failure'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
