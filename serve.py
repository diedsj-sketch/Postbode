"""Supervise the receiver and optional worker on one persistent Railway volume."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def run_startup_connection_check(env, checker=None):
    """Run the opt-in, read-only provider check before starting services."""
    if env.get('CHECK_CONNECTIONS_ON_START') != 'true':
        return None
    if checker is None:
        from mailroom import check_connections
        checker = check_connections
    result = checker()
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def commands(env):
    port = int(env.get('PORT', '8080'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    result = [('receiver', [sys.executable, '-m', 'gunicorn', '--bind', f'0.0.0.0:{port}',
                          '--workers', '1', '--threads', '2', '--timeout', '60',
                          'mailroom:application'])]
    if env.get('PROCESSING_ENABLED') == 'true':
        required = ['OPENAI_API_KEY', 'POSTBODE_WEBHOOK_SECRET', 'POSTBODE_RECIPIENT_UUID', 'ALERT_EMAIL']
        if any(not env.get(k) for k in required):
            raise ValueError('Processing configuration incomplete')
        if not (env.get('GOOGLE_TOKEN_JSON') or env.get('GOOGLE_TOKEN_FILE')):
            raise ValueError('Google authorization missing')
        if env.get('ENABLE_CALENDAR') == 'true' and not env.get('GOOGLE_CALENDAR_ID'):
            raise ValueError('Personal calendar missing')
        result.append(('worker', [sys.executable, 'mailroom.py', 'worker']))
    return result


def prepare_volume():
    os.umask(0o077)
    path = Path(os.environ.get('DATA_DIR', '/data'))
    # Railway supplies this variable for the mounted volume. Fail closed if it is absent.
    if os.environ.get('RAILWAY_ENVIRONMENT_ID'):
        mounted = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH')
        if not mounted or Path(mounted).resolve() != path.resolve():
            raise RuntimeError('Persistent volume must be mounted at DATA_DIR')
    if path.is_symlink():
        raise RuntimeError('Data directory cannot be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        # The volume is initially root-owned. Change its top-level owner, then drop privileges.
        import pwd
        user = pwd.getpwnam('mailroom')
        os.chown(path, user.pw_uid, user.pw_gid)
        os.chmod(path, 0o700)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    # Also prove the mounted directory is writable before reporting a healthy deployment.
    with tempfile.TemporaryFile(dir=path) as f:
        f.write(b'ok')
        f.flush()
        os.fsync(f.fileno())


def supervise(specs):
    children = []
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        for name, cmd in specs:
            children.append((name, subprocess.Popen(cmd, start_new_session=True)))
        while not stopping:
            for name, child in children:
                if child.poll() is not None:
                    print(f'{name} exited; restarting service', flush=True)
                    return 1
            time.sleep(0.25)
        return 0
    finally:
        for name, child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 15
        for name, child in children:
            try:
                child.wait(timeout=max(0.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        for s, handler in previous.items():
            signal.signal(s, handler)


if __name__ == '__main__':
    try:
        specs = commands(os.environ)
        prepare_volume()
        run_startup_connection_check(os.environ)
    except Exception:
        # Do not expose environment contents or secrets in deployment logs.
        print('Startup configuration or persistent-volume check failed', file=sys.stderr)
        sys.exit(1)
    sys.exit(supervise(specs))
