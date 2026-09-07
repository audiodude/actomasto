from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class SourceCancelled(Exception):
    """A control/configuration boundary invalidated a source iteration."""

def locations():
    home = Path.home()
    return {
        'config': Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'actomasto',
        'data': Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'actomasto',
        'runtime': Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'actomasto',
        'systemd': Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'systemd/user',
    }


def secure_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('unsafe_directory')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise RuntimeError('directory_owner_mismatch')
    path.chmod(0o700)
    return path


@contextmanager
def writer_lock(data_dir):
    path = secure_dir(data_dir) / 'writer.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('daemon_writer_active') from None
        yield
    finally:
        os.close(fd)


def control(command, **arguments):
    path = locations()['runtime'] / 'control.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(180)
        client.connect(str(path))
        client.sendall(json.dumps({'command': command, **arguments}).encode() + b'\n')
        data = bytearray()
        while not data.endswith(b'\n'):
            part = client.recv(65536)
            if not part:
                raise RuntimeError('control_connection_closed')
            data.extend(part)
            if len(data) > 16 * 1024 * 1024:
                raise RuntimeError('control_response_oversized')
        result = json.loads(data)
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result['result']


def terminal_safe(value):
    if isinstance(value, str):
        return ''.join(c if c in '\n\t' or unicodedata.category(c)[0] != 'C' else f'\\u{ord(c):04x}' for c in value)
    if isinstance(value, list):
        return [terminal_safe(v) for v in value]
    if isinstance(value, dict):
        return {terminal_safe(str(k)): terminal_safe(v) for k, v in value.items()}
    return value


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp_requires_timezone')
    return parsed.astimezone(timezone.utc).timestamp()


def credential():
    value = os.environ.get('ANTHROPIC_API_KEY')
    if value:
        return value
    path = locations()['config'] / 'credentials.env'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe_credential_permissions')
        for line in stream:
            name, separator, value = line.strip().partition('=')
            if separator and name == 'ANTHROPIC_API_KEY':
                return value.strip().strip('\"\'') or None
    return None
