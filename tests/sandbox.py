"""Test-only environment and filesystem guards; never import application code here."""
import hashlib
import json
import os
from pathlib import Path
import sys
import shlex

PATH_ENV = ('HOME', 'USERPROFILE', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR',
            'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
            'XDG_RUNTIME_DIR', 'APPDATA', 'LOCALAPPDATA', 'CC_RELAY_HUB_DIR',
            'CC_CONFIG', 'HOOK_EVENTS_FILE', 'TMPDIR', 'TMP', 'TEMP')


def under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, TypeError):
        return False


def protected_paths(env=None):
    env = os.environ if env is None else env
    home = Path(env.get('CC_RELAY_TEST_REAL_HOME') or Path.home()).resolve()
    hub = home / '.cc-connect' / 'cc-relay-hub'
    paths = [home/'.claude/CLAUDE.md', home/'.codex/AGENTS.md',
             hub/'registry.json', hub/'bindings.json', hub/'state.db']
    # Also protect non-default production locations without losing the defaults.
    if not env.get('CC_RELAY_TEST_ROOT'):
        if env.get('CODEX_HOME'):
            paths.append(Path(env['CODEX_HOME'])/'AGENTS.md')
        if env.get('CC_RELAY_HUB_DIR'):
            paths += [Path(env['CC_RELAY_HUB_DIR'])/name for name in ('registry.json','bindings.json','state.db')]
    return list(dict.fromkeys(str(p.resolve()) for p in paths))


def fingerprint(path):
    path = Path(path)
    try:
        stat = path.stat()
        return {'exists': True, 'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    except FileNotFoundError:
        return {'exists': False}


def snapshot(paths):
    return {str(path): fingerprint(path) for path in paths}


def verify_snapshot(before):
    after = snapshot(before)
    changed = [path for path in before if before[path] != after[path]]
    return {'ok': not changed, 'changed': changed, 'before': before, 'after': after}


def make_environment(folder, source, parent=None):
    parent = os.environ if parent is None else parent
    root, source = Path(folder).resolve(), Path(source).resolve()
    home = root/'home'
    locations = {
        'HOME': home, 'USERPROFILE': home, 'CODEX_HOME': home/'.codex',
        'CLAUDE_CONFIG_DIR': home/'.claude', 'XDG_CONFIG_HOME': home/'.config',
        'XDG_DATA_HOME': home/'.local/share', 'XDG_CACHE_HOME': home/'.cache',
        'XDG_STATE_HOME': home/'.local/state', 'XDG_RUNTIME_DIR': root/'run',
        'APPDATA': home/'AppData/Roaming', 'LOCALAPPDATA': home/'AppData/Local',
        'CC_RELAY_HUB_DIR': home/'.cc-connect/cc-relay-hub',
        'TMPDIR': root/'tmp', 'TMP': root/'tmp', 'TEMP': root/'tmp',
    }
    for path in locations.values(): path.mkdir(parents=True, exist_ok=True)
    env = dict(parent)
    # Never inherit the caller's live identity, launcher, hook address or config.
    for name in list(env):
        if name.startswith('CC_') or name in ('HOOK_PORT','PYTHONPATH','PYTHON_BIN','HOOK_EVENTS_FILE'):
            env.pop(name)
    env.update({name: str(path) for name, path in locations.items()})
    env.update({
        'CC_CONFIG': str(home/'.cc-connect/config.toml'),
        'HOOK_EVENTS_FILE': str(root/'events.jsonl'),
        'HOMEDRIVE': home.drive, 'HOMEPATH': str(home)[len(home.drive):],
        'CC_RELAY_TEST_ROOT': str(root),
        'CC_RELAY_TEST_REAL_HOME': parent.get('CC_RELAY_TEST_REAL_HOME') or str(Path.home().resolve()),
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': os.pathsep.join([str(source/'tests/_guard'), str(source)]),
    })
    # Keep CLI/hook subprocesses on this interpreter instead of a different
    # system Python that relied on packages from the real user's site directory.
    if os.name == 'posix':
        bindir = root/'bin'; bindir.mkdir(exist_ok=True)
        for name in ('python3', 'python'):
            launcher = bindir/name
            launcher.write_text('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
            launcher.chmod(0o700)
        env['PATH'] = str(bindir) + os.pathsep + parent.get('PATH','')
    env['PYTHON_BIN'] = sys.executable
    hub = locations['CC_RELAY_HUB_DIR']
    (hub/'registry.json').write_text(json.dumps({'version':2,'agents':{},'groups':{}}))
    (hub/'bindings.json').write_text(json.dumps({'cc_connect':{},'cdp':{}}))
    (root/'.relay-test-sandbox').write_text('temporary relay test environment\n')
    return env


def require_sandbox(env=None):
    env = os.environ if env is None else env
    root = env.get('CC_RELAY_TEST_ROOT')
    if not root or not (Path(root)/'.relay-test-sandbox').is_file():
        raise RuntimeError('Tests require a temporary HOME: run python3 -B tests/run_isolated.py')
    for name in PATH_ENV:
        if not env.get(name) or not under(env[name], root):
            raise RuntimeError('Unsafe test environment: %s is outside the temporary sandbox' % name)
    if env is os.environ:
        if Path.home().resolve() != Path(env['HOME']).resolve() or Path(os.path.expanduser('~')).resolve() != Path(env['HOME']).resolve():
            raise RuntimeError('Path.home()/expanduser do not resolve to the temporary HOME')
    return Path(root)


_guard_installed = False


def install_write_guard():
    """Inherited by Python children via test-only sitecustomize. Fail before writes.

    This is an accident guard, not a security boundary against hostile code.
    Subprocesses still inherit the temporary HOME and other redirected paths.
    """
    global _guard_installed
    if _guard_installed: return
    root = require_sandbox()
    real_home = Path(os.environ['CC_RELAY_TEST_REAL_HOME']).resolve()
    sys.dont_write_bytecode = True
    def check(path):
        if not isinstance(path, (str, bytes, os.PathLike)): return
        path = os.fsdecode(path)
        if under(path, real_home) and not under(path, root):
            raise PermissionError('Test attempted to modify the real user directory: %s' % path)
    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                check(path)
        elif event == 'sqlite3.connect':
            path = args[0]
            if isinstance(path,str) and path.startswith('file:'): path = path[5:].split('?',1)[0]
            check(path)
        elif event in ('os.remove','os.rmdir','os.mkdir','os.chmod','os.chown','os.utime','os.truncate'):
            check(args[0])
        elif event in ('os.rename','os.link','os.symlink'):
            check(args[0]); check(args[1])
        elif event == 'subprocess.Popen':
            # An explicitly supplied env must not restore a production HOME.
            if args[3] is not None: require_sandbox(args[3])
    sys.addaudithook(audit)
    _guard_installed = True
