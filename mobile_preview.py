"""Start an isolated mobile preview without inheriting integration credentials."""

import argparse
from contextlib import contextmanager
import fcntl
import ipaddress
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile


def preview_secret(root):
    path = root / '.session-secret'
    if path.exists():
        value = path.read_text(encoding='ascii').strip()
        if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
            raise ValueError('Ключ тестового сховища пошкоджено. Відновіть наявний ключ із резервної копії.')
        return value

    value = secrets.token_hex(32)
    with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=root, delete=False) as pending:
        try:
            pending.write(value + '\n')
            pending.flush()
            os.fsync(pending.fileno())
            os.replace(pending.name, path)
        finally:
            Path(pending.name).unlink(missing_ok=True)
    return value


@contextmanager
def preview_runtime(path, *, https_proxy=False):
    """Keep the session key, HOLD and checkpoint media together on one local disk."""
    password = os.environ.get('VINTAGE_OPERATOR_PASSWORD')
    if https_proxy and (path is None or not password):
        raise ValueError('Для тесту через HTTPS задайте --data-dir і VINTAGE_OPERATOR_PASSWORD.')
    if path is None:
        with tempfile.TemporaryDirectory(prefix='vintage-mobile-') as temporary:
            with preview_runtime(Path(temporary)) as runtime:
                yield runtime
        return

    root = path.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / '.preview.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Це сховище вже використовує інший екземпляр тестового сервера.') from exc
        env = {
            'APP_SECRET': preview_secret(root),
            'XDG_RUNTIME_DIR': str(root),
            'ALLOW_PAID_AI': '0',
            'ENABLE_LIVE_PUBLISH': '0',
            'ACTIVE_LOT_CHECKPOINT_DIR': str(root / 'checkpoints'),
            'HOLD_QUEUE_DIR': str(root / 'hold'),
        }
        if password:
            env['VINTAGE_OPERATOR_PASSWORD'] = password
        if https_proxy:
            env['VINTAGE_SECURE_COOKIES'] = '1'
        for name in ('VINTAGE_HOLD_BACKUP_KEY', 'RENDER_GIT_COMMIT'):
            if os.environ.get(name):
                env[name] = os.environ[name]
        if 'PYTHONPATH' in os.environ:
            env['PYTHONPATH'] = os.environ['PYTHONPATH']
        yield env, lock.fileno()


def run_preview(host, port, env, lock_fd):
    command = [
        sys.executable, '-m', 'gunicorn', '--workers', '1', '--threads', '4',
        '--bind', f'{host}:{port}', 'app:app',
    ]
    # Keep the data lock held by Gunicorn even if the launcher is killed.
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parent, env=env, pass_fds=(lock_fd,)) as process:
        def forward(signum, _frame):
            if process.poll() is None:
                process.send_signal(signum)

        handlers = {signum: signal.signal(signum, forward) for signum in (signal.SIGTERM, signal.SIGINT)}
        try:
            return process.wait()
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=5000, type=int)
    parser.add_argument('--data-dir', type=Path, default=os.environ.get('VINTAGE_PREVIEW_DATA_DIR') or None,
                        help='Тривале локальне сховище для тестових фото, сесій і HOLD.')
    parser.add_argument('--https-proxy', action='store_true',
                        help='Позначити сесію як HTTPS за локальним зворотним проксі.')
    args = parser.parse_args()
    try:
        if args.https_proxy and not ipaddress.ip_address(args.host).is_loopback:
            raise ValueError('Для локального HTTPS-проксі використайте loopback, наприклад 127.0.0.1.')
        with preview_runtime(args.data_dir, https_proxy=args.https_proxy) as (env, lock_fd):
            return run_preview(args.host, args.port, env, lock_fd)
    except (OSError, ValueError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
