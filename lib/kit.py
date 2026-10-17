"""Portable installer. All user-specific data lives outside the source directory."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
SERVICE = 'model-switch-gateway.service'
LABEL = 'local.model-switch.gateway'
PROVIDER = 'model_switch_cursor'


def atomic(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('拒绝覆盖符号链接: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = content.encode() if isinstance(content, str) else content
    if path.is_file() and path.read_bytes() == data:
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def dump(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def run(argv, timeout=60, env=None):
    r = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                       timeout=timeout, env=env)
    if r.returncode:
        # Do not echo plugin/auth command output, which can contain credentials.
        raise ValueError(f'{Path(str(argv[0])).name} {argv[1] if len(argv)>1 else ""} 失败，退出码 {r.returncode}')
    return r.stdout


def provider_off(magpie, provider, env):
    """Disable a built-in provider when present; a fresh magpie may not have it yet."""
    r = subprocess.run([str(magpie), 'provider', 'off', provider], capture_output=True, text=True,
                       timeout=60, env=env)
    if r.returncode and 'no provider' not in r.stdout + r.stderr:
        raise ValueError(f'magpie provider off {provider} 失败，退出码 {r.returncode}')


def platform_key(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = machine or platform.machine()
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    if system not in ('linux', 'darwin') or not arch:
        raise ValueError('支持 Linux/macOS x64/arm64；Windows 请在 WSL2 内运行。')
    return system + '-' + arch


def verify(path, spec):
    algo = 'sha256' if 'sha256' in spec else 'sha512'
    h = hashlib.new(algo)
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    actual = h.hexdigest() if algo == 'sha256' else 'sha512-' + base64.b64encode(h.digest()).decode()
    if actual != spec.get('sha256', spec.get('integrity')):
        raise ValueError('依赖校验失败，未执行: ' + str(path))


def download(spec, cache, vendor):
    supplied = vendor / spec['file']
    if supplied.is_file():
        verify(supplied, spec)
        return supplied
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / spec['file']
    if target.exists():
        verify(target, spec)
        return target
    fd, name = tempfile.mkstemp(dir=cache, prefix='.download-')
    os.close(fd)
    try:
        run(['curl', '-fLsS', '--retry', '2', '--connect-timeout', '20', '--max-time', '600',
             spec['url'], '-o', name], timeout=620)
        verify(name, spec)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return target


def extract(archive, target):
    # Refuse traversal, device files and escaping symlinks before extracting anything.
    with tarfile.open(archive) as t:
        members = t.getmembers()
        for member in members:
            dest = (target / member.name).resolve()
            if not dest.is_relative_to(target.resolve()) or member.isdev() or member.isfifo():
                raise ValueError('不安全的归档路径: ' + member.name)
            if member.issym() or member.islnk():
                link = ((target / member.name).parent if member.issym() else target) / member.linkname
                if not link.resolve().is_relative_to(target.resolve()):
                    raise ValueError('归档符号链接越界: ' + member.name)
        t.extractall(target, members=members, filter='data')


def tool_health(executable, version):
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('依赖缺失或不可执行: ' + str(executable))
    output = run([executable, '--version'], timeout=30)
    if not any(word.removeprefix('v') == version for word in output.split()):
        raise ValueError('依赖实际版本与锁定版本不一致: ' + str(executable))


def cached_tool(name, spec, prefix, version):
    target = prefix / 'tools' / (name + '-' + version)
    marker = target / '.installed.json'
    if marker.exists():
        saved = json.loads(marker.read_text())
        if saved.get('spec') != spec:
            raise ValueError('依赖版本目录与锁文件不一致: ' + str(target))
        executable = target / saved['executable']
        if not executable.resolve().is_relative_to(target.resolve()):
            raise ValueError('依赖安装记录的可执行路径越界: ' + str(target))
        tool_health(executable, version)
        return executable
    if target.exists():
        raise ValueError('依赖目录已存在但没有安装记录，请检查: ' + str(target))
    return None


def install_tool(name, spec, prefix, cache, vendor, version):
    existing = cached_tool(name, spec, prefix, version)
    if existing is not None:
        return existing
    target = prefix / 'tools' / (name + '-' + version)
    print('准备依赖: ' + name + ' ' + version, flush=True)
    archive = download(spec, cache, vendor)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.unpack-') as d:
        stage = Path(d)
        if name == 'magpie':
            shutil.copyfile(archive, stage / 'magpie')
            (stage / 'magpie').chmod(0o755)
            relative = 'magpie'
            atomic(stage / '.portable', '')
        else:
            extract(archive, stage)
            relative = spec['executable'] if name == 'codex' else 'dist-package/cursor-agent'
        binary = stage / relative
        tool_health(binary, version)
        dump(stage / '.installed.json', {'spec': spec, 'executable': relative})
        # Move files out of the temporary wrapper directory, atomically as one tree.
        os.replace(stage, target)
    return target / relative


def profile_text(values):
    return ''.join(f'{k} = {json.dumps(v, ensure_ascii=False)}\n' for k, v in values.items())


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def tool_env(state):
    env = os.environ.copy()
    env.update(DO_NOT_TRACK='1', MAGPIE_NO_STATS='1',
               MAGPIE_ADDR='127.0.0.1:' + str(state['port']),
               PATH=str(Path(state['prefix']) / 'bin') + os.pathsep + env.get('PATH', ''))
    return env


def load_state(path=None):
    path = Path(path or os.environ.get('MODEL_SWITCH_STATE', SOURCE / 'state.json'))
    if not path.is_file():
        raise ValueError('尚未安装完成；请先运行 bash ./install。')
    return json.loads(path.read_text())


def gateway_url(state):
    return 'http://127.0.0.1:' + str(state['port'])


def user_systemd():
    if sys.platform != 'linux' or not shutil.which('systemctl'):
        return False
    return subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True, timeout=10).returncode == 0


def check_service(state, requested):
    """Read-only service selection and ownership checks, also reused before writes."""
    if requested == 'none':
        return 'none', None
    prefix = Path(state['prefix'])
    launcher = prefix / 'bin' / 'model-switch-gateway'
    if user_systemd():
        path = Path.home() / '.config/systemd/user' / SERVICE
        if path.is_symlink() or (path.exists() and not path.read_text().startswith('# Managed by model-switch-kit: ' + str(prefix) + '\n')):
            raise ValueError('已有其他安装包占用服务名称: ' + str(path))
        return 'systemd', path
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
        if path.is_symlink() or (path.exists() and plistlib.loads(path.read_bytes()).get('ProgramArguments') != [str(launcher)]):
            raise ValueError('已有其他安装包占用 launchd 名称。')
        return 'launchd', path
    raise ValueError('没有可用的用户服务管理器。容器/无 systemd 的机器请加 --service none；随后手动运行 model-switch-gateway。')


def install_service(state, requested):
    manager, path = check_service(state, requested)
    if manager == 'none':
        return manager
    prefix = Path(state['prefix'])
    launcher = prefix / 'bin' / 'model-switch-gateway'
    if manager == 'systemd':
        # JSON-style quoting is accepted by systemd. Escape specifier expansion too.
        exe = json.dumps(str(launcher).replace('%', '%%'))
        content = ('# Managed by model-switch-kit: ' + str(prefix) + '\n'
                   '[Unit]\nDescription=Cursor gateway for model-switch\nAfter=network-online.target\n'
                   '[Service]\nType=simple\nExecStart=' + exe + '\nUMask=0077\nRestart=on-failure\nRestartSec=3\n'
                   '[Install]\nWantedBy=default.target\n')
        atomic(path, content)
        run(['systemctl', '--user', 'daemon-reload'])
        run(['systemctl', '--user', 'enable', '--now', SERVICE])
        return 'systemd'
    if manager == 'launchd':
        value = {'Label': LABEL, 'ProgramArguments': [str(launcher)], 'RunAtLoad': True, 'KeepAlive': True,
                 'StandardOutPath': str(prefix / 'gateway.log'), 'StandardErrorPath': str(prefix / 'gateway.log')}
        atomic(path, plistlib.dumps(value))
        domain = 'gui/' + str(os.getuid())
        loaded = subprocess.run(['launchctl', 'print', domain + '/' + LABEL], capture_output=True, timeout=10)
        if loaded.returncode:
            run(['launchctl', 'bootstrap', domain, str(path)])
        return 'launchd'


def gateway_ready(state, start=False):
    if start and state.get('service') == 'systemd':
        run(['systemctl', '--user', 'start', SERVICE])
    elif start and state.get('service') == 'launchd':
        run(['launchctl', 'kickstart', 'gui/' + str(os.getuid()) + '/' + LABEL])
    try:
        return get_json(gateway_url(state) + '/v1/models')
    except OSError as exc:
        raise ValueError('网关不可用；运行 model-switch-gateway 或检查服务。') from exc


def cursor_login_ok(state):
    r = subprocess.run([state['agent'], 'status'], capture_output=True, text=True,
                       timeout=30, env=tool_env(state))
    return r.returncode == 0 and 'Logged in' in r.stdout + r.stderr


def provider_config(state):
    return {'name': 'Cursor via model-switch-kit', 'base_url': gateway_url(state) + '/v1',
            'wire_api': 'responses', 'experimental_bearer_token': 'magpie'}


def check_config(root, provider):
    from model_switch import KEYS
    for name in ('config.toml', 'cursor.config.toml', 'openai.config.toml',
                 'original.config.toml', 'model-switch-models.json'):
        path = root / name
        if path.is_symlink():
            raise ValueError(name + ' 是符号链接，拒绝写入。')
        if path.exists() and not path.is_file():
            raise ValueError('目标不是普通文件: ' + str(path))
    config = root / 'config.toml'
    old = config.read_text() if config.exists() else ''
    current = tomllib.loads(old)
    registered = current.get('model_providers', {}).get(PROVIDER)
    if registered is not None and registered != provider:
        raise ValueError('provider 名称冲突，未覆盖: ' + PROVIDER)
    if current.get('profile'):
        raise ValueError('根配置已有 profile 覆盖；请先确认其优先级，未修改。')
    cursor_path = root / 'cursor.config.toml'
    if cursor_path.exists() and set(tomllib.loads(cursor_path.read_text())) - KEYS:
        raise ValueError('已有 cursor profile 含非模型配置，拒绝覆盖；请先确认。')
    return old, current


def configure(state, catalog, models, default_alias):
    from model_switch import CURSOR_MODELS, KEYS, model_defaults, prefer_defaults, render
    root, prefix = Path(state['codex_dir']), Path(state['prefix'])
    offered = {m['id']: m for m in models.get('data', [])}
    target = 'cursor-plugin/' + CURSOR_MODELS[default_alias]
    entries = [m for m in catalog.get('models', []) if m['slug'] == target]
    if target not in offered or len(entries) != 1:
        raise ValueError('账号/网关未提供默认模型 ' + target + '；Codex 配置未修改。')
    window = entries[0].get('max_context_window') or entries[0].get('context_window')
    if not isinstance(window, int) or isinstance(window, bool) or not 1000 <= window <= offered[target].get('context_window', 0):
        raise ValueError('网关与模型目录上下文声明不一致；配置未修改。')
    window, effort = model_defaults(entries[0])
    catalog = prefer_defaults(catalog)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider = provider_config(state)
    with (root / '.model-switch-restart.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = root / 'config.toml'
        old, current = check_config(root, provider)
        registered = current.get('model_providers', {}).get(PROVIDER)
        if registered is None:
            new = old.rstrip() + '\n\n[model_providers.' + PROVIDER + ']\n' + profile_text(provider)
        else:
            new = old
        values = {
            'model': target, 'model_provider': PROVIDER,
            'model_catalog_json': str(root / 'model-switch-models.json'),
            'model_context_window': window, 'model_auto_compact_token_limit': min(512000, window * 3 // 4),
            'model_reasoning_effort': effort, 'model_reasoning_summary': 'none', 'web_search': 'disabled',
            'openai_base_url': gateway_url(state) + '/backend-api/codex',
        }
        cursor = profile_text({k: v for k, v in values.items() if v is not None})
        # Installation registers the provider but does not switch or restart live sessions.
        render(new, cursor)
        changes = {config: new,
                   root / 'model-switch-models.json': json.dumps(catalog, ensure_ascii=False, indent=2) + '\n',
                   root / 'cursor.config.toml': cursor}
        original = root / 'original.config.toml'
        if not original.exists():
            changes[original] = profile_text({k: v for k, v in current.items() if k in KEYS})
        openai = root / 'openai.config.toml'
        if not openai.exists():
            values = ({k: v for k, v in current.items() if k in KEYS}
                      if current.get('model_provider', 'openai') == 'openai' else {'model_provider': 'openai'})
            changes[openai] = profile_text(values)
        snapshots = {p: p.read_bytes() if p.exists() else None for p in changes}
        backup = prefix / 'backups' / 'before-first-config'
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (backup / 'manifest.json').exists():
            saved = {}
            for p, data in snapshots.items():
                saved[p.name] = data is not None
                if data is not None:
                    atomic(backup / p.name, data)
            dump(backup / 'manifest.json', saved)
        written = []
        try:
            for p, data in changes.items():
                if p.is_symlink() or (p.read_bytes() if p.exists() else None) != snapshots[p]:
                    raise ValueError('配置被其他进程修改，取消安装: ' + str(p))
                atomic(p, data)
                written.append(p)
        except Exception:
            for p in reversed(written):
                # Do not undo a concurrent user's write.
                if p.read_bytes() == changes[p].encode():
                    if snapshots[p] is None:
                        p.unlink()
                    else:
                        atomic(p, snapshots[p])
            raise
    return sorted(a for a, model in CURSOR_MODELS.items() if 'cursor-plugin/' + model in offered)


def install_wrappers(state, bin_dir):
    prefix = Path(state['prefix'])
    private = prefix / 'bin'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = shlex.quote(state['python'])
    state_path = shlex.quote(str(prefix / 'state.json'))
    common = '#!/bin/sh\n# model-switch-kit\nexport MODEL_SWITCH_STATE=' + state_path + '\n'
    wrappers = {
        'model-switch': common + 'exec ' + python + ' -B ' + shlex.quote(str(prefix / 'lib/model_switch.py')) + ' "$@"\n',
        'codex': '#!/bin/sh\n# model-switch-kit\nexec ' + shlex.quote(state['codex']) + ' "$@"\n',
        'agent': '#!/bin/sh\n# model-switch-kit\nexec ' + shlex.quote(state['agent']) + ' "$@"\n',
        'cursor-agent': '#!/bin/sh\n# model-switch-kit\nexec ' + shlex.quote(state['agent']) + ' "$@"\n',
        'model-switch-gateway': common + 'exec ' + python + ' -B ' + shlex.quote(str(prefix / 'lib/kit.py')) + ' --serve\n',
    }
    # Private magpie is needed by both the plugin runner and diagnostic commands.
    wrappers['magpie'] = '#!/bin/sh\n# model-switch-kit\nexec ' + shlex.quote(state['magpie']) + ' "$@"\n'
    for name, content in wrappers.items():
        atomic(private / name, content, 0o755)
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    skipped = []
    for name in wrappers:
        dest, target = bin_dir / name, private / name
        if dest.absolute() == target.absolute():
            continue
        if dest.is_symlink() and dest.resolve() == target.resolve():
            continue
        if dest.exists() or dest.is_symlink():
            skipped.append(name)
            continue
        dest.symlink_to(target)
    if skipped:
        print('未覆盖已有命令: ' + ', '.join(skipped) + '；本安装包入口位于 ' + str(private))
    # No shell startup file is silently modified.
    atomic(prefix / 'env.sh', 'export PATH=' + shlex.quote(str(private)) + ':"$PATH"\n')


def writable_directory(path):
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise ValueError('目录不可写或父路径不是目录: ' + str(path))


def preflight(args, prefix, root, specs, lock):
    """No downloads, config writes or service starts; login/model checks come later."""
    if prefix == Path.home() or prefix == Path('/') or prefix == SOURCE or prefix.is_relative_to(SOURCE):
        raise ValueError('安装目录必须是独立私有目录，不能是 HOME、根目录或 Git 源码目录。')
    if not shutil.which('curl'):
        raise ValueError('缺少 curl，请先安装系统基础依赖。')
    writable_directory(prefix)
    if args.download_only:
        writable_directory(Path(args.vendor_dir or SOURCE / 'vendor').expanduser().resolve())
        return {}
    saved = prefix / 'state.json'
    state = json.loads(saved.read_text()) if saved.exists() else {}
    if state and (state['port'] != args.port or state['codex_dir'] != str(root) or state['prefix'] != str(prefix)):
        raise ValueError('重装参数与原安装不一致；请使用原 --prefix / --codex-dir / --port。')
    planned = dict(state, prefix=str(prefix), codex_dir=str(root), port=args.port)
    writable_directory(root)
    writable_directory(Path(args.bin_dir or Path.home() / '.local/bin').expanduser().resolve())
    check_config(root, provider_config(planned))
    _, service_path = check_service(planned, args.service)
    if service_path is not None:
        writable_directory(service_path.parent)
    if not state:
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', args.port))
            except OSError as exc:
                raise ValueError('端口已占用；不会接管现有网关。请用 --port 指定空闲端口。') from exc
    for name, spec in specs.items():
        cached_tool(name, spec, prefix, lock[name + '_version'])
    return state


def setup(args):
    from model_switch import CURSOR_MODELS
    key = platform_key()
    lock = json.loads((SOURCE / 'deps.lock.json').read_text())
    prefix = Path(args.prefix).expanduser().resolve()
    root = Path(args.codex_dir or os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()
    vendor = Path(args.vendor_dir or SOURCE / 'vendor').expanduser().resolve()
    specs = lock['platforms'][key]
    preflight(args, prefix, root, specs, lock)
    if args.check:
        print(json.dumps({'platform': key, 'prefix': str(prefix), 'codex_dir': str(root),
                          'python': sys.executable, 'curl': shutil.which('curl'),
                          'user_systemd': user_systemd(), 'port': args.port,
                          'versions': {k: v for k, v in lock.items() if k.endswith('_version')}}, indent=2))
        print('基础预检通过；登录和账号模型可用性在依赖就绪后检查。')
        return 0
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix.chmod(0o700)
    with (prefix / '.install.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        state = preflight(args, prefix, root, specs, lock)
        cache = prefix / 'downloads'
        if args.download_only:
            vendor.mkdir(parents=True, exist_ok=True)
            for spec in specs.values():
                src = download(spec, cache, vendor)
                if src != vendor / spec['file']:
                    shutil.copyfile(src, vendor / spec['file'])
            print('依赖包已保存: ' + str(vendor) + '；首次插件安装和登录仍需联网。')
            return 0
        saved_path = prefix / 'state.json'
        binaries = {name: str(install_tool(name, spec, prefix, cache, vendor, lock[name + '_version']))
                    for name, spec in specs.items()}
        state.update(schema=1, prefix=str(prefix), codex_dir=str(root), port=args.port,
                     python=sys.executable, codex=binaries['codex'], agent=binaries['cursor'],
                     magpie=binaries['magpie'], default=args.model)
        for file in ('kit.py', 'model_switch.py'):
            atomic(prefix / 'lib' / file, (SOURCE / 'lib' / file).read_bytes())
        atomic(prefix / 'deps.lock.json', (SOURCE / 'deps.lock.json').read_bytes())
        dump(saved_path, state)
        install_wrappers(state, Path(args.bin_dir or Path.home() / '.local/bin').expanduser().resolve())
        print('CLI 已安装。当前终端启用: source ' + shlex.quote(str(prefix / 'env.sh')), flush=True)
        if not cursor_login_ok(state):
            print('等待 Cursor 登录（退出码 3）。运行: ' + shlex.quote(str(prefix / 'bin/agent')) + ' login\n登录后重新执行相同 install 命令；Codex 配置尚未修改。')
            return 3
        magpie = Path(state['magpie'])
        data = magpie.parent / 'data'
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings_path = data / 'settings.json'
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        settings.update(noStats=True, noUsageStats=True, noAutoUpdate=True, lan=False)
        dump(settings_path, settings)
        env = tool_env(state)
        if state.get('plugin') != lock['cursor_plugin']:
            print('安装固定版本 Cursor 适配插件和 magpie 管理的 Bun runtime…', flush=True)
            run([magpie, 'plugin', 'add', lock['cursor_plugin']], timeout=600, env=env)
            state['plugin'] = lock['cursor_plugin']
            dump(saved_path, state)
        run([magpie, 'plugin', 'login', 'cursor', '2'], timeout=120, env=env)
        provider_off(magpie, 'codex', env)
        provider_off(magpie, 'cursor', env)
        run([magpie, 'provider', 'models', 'cursor-plugin'], timeout=120, env=env)
        providers = json.loads((data / 'plugin-providers.json').read_text())
        available = {m['id'] for p in providers if p['id'] == 'cursor' for m in p['models']}
        selected = [model for model in CURSOR_MODELS.values() if model in available]
        if CURSOR_MODELS[args.model] not in available:
            raise ValueError('账号尚未提供 ' + args.model + '，未切换/修改 Codex 配置。')
        run([magpie, 'provider', 'models', 'cursor-plugin', *selected], env=env)
        state['service'] = install_service(state, args.service)
        dump(saved_path, state)
        if state['service'] == 'none':
            try:
                gateway_ready(state)
            except ValueError:
                print('等待网关启动（退出码 3）。在另一终端运行 ' + shlex.quote(str(prefix / 'bin/model-switch-gateway')) + '，再重跑 install。')
                return 3
        for attempt in range(30):
            try:
                models = gateway_ready(state)
                catalog = get_json(gateway_url(state) + '/v1/codex/models')
                break
            except (ValueError, OSError):
                if attempt == 29:
                    raise ValueError('网关未就绪，Codex 配置未修改；请检查 gateway 服务。')
                time.sleep(1)
        state['models'] = configure(state, catalog, models, args.model)
        state['configured'] = True
        dump(saved_path, state)
        print('安装完成。可用别名: ' + ', '.join(state['models']))
        print('未改变默认模型、未重启现有会话。启用: model-switch codex cursor')
        print('切回 OpenAI: model-switch codex openai；恢复安装前模型配置: model-switch codex original')
        print('OpenAI 未登录时使用 codex login；本安装过程不发起模型推理。')
        return 0


def main():
    parser = argparse.ArgumentParser(description='从任意目录安装 Codex + Cursor CLI + 本地网关；不安装桌面 App。')
    parser.add_argument('--prefix', default=os.environ.get('MODEL_SWITCH_PREFIX', str(Path.home() / '.local/share/model-switch-kit')))
    parser.add_argument('--codex-dir', help='Codex 配置目录；默认读取现有 CODEX_HOME 或 ~/.codex')
    parser.add_argument('--bin-dir', help='命令链接目录；已有命令不会覆盖')
    parser.add_argument('--vendor-dir', help='本地依赖包目录；默认源码 vendor/')
    parser.add_argument('--port', type=int, default=3425)
    parser.add_argument('--service', choices=['auto', 'none'], default='auto')
    parser.add_argument('--model', choices=['opus5.5', 'sonnet5.5', 'haiku5.5', 'gemini4', 'gemini3.8flash', 'luna5.6'],
                        default='opus5.5')
    parser.add_argument('--check', action='store_true', help='只检查，不安装/改配置')
    parser.add_argument('--download-only', action='store_true', help='把当前平台三项依赖包保存到 vendor/')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.serve:
            state = load_state()
            os.execve(state['magpie'], [state['magpie'], 'serve'], tool_env(state))
        if not 1024 <= args.port <= 65535:
            raise ValueError('--port 必须在 1024..65535 之间。')
        return setup(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print('安装未完成: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
