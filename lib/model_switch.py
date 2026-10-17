#!/usr/bin/env python3
"""Switch Codex model settings without replacing MCP, auth, or other settings."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import json
import urllib.request
from kit import (load_state, tool_env, cursor_login_ok, gateway_ready, gateway_url, PROVIDER)

KEYS = {
    "model", "model_provider", "model_catalog_json", "model_context_window",
    "model_auto_compact_token_limit", "model_reasoning_effort",
    "model_reasoning_summary", "web_search", "openai_base_url",
}
BEGIN = "# BEGIN model-switch provider"
END = "# END model-switch provider"
BLOCK = re.compile(r"(?m)^" + re.escape(BEGIN) + r"\n.*?^" + re.escape(END) + r"\n?", re.S)
HEADER = re.compile(r"(?m)^[ \t]*\[")
SETTING = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z_0-9]*)[ \t]*=")
CURSOR_MODELS = {
    "opus5.5": "claude-opus-5-5@1m",
    "sonnet5.5": "claude-sonnet-5-5@1m",
    "haiku5.5": "claude-haiku-5-5@1m",
    "gemini4": "gemini-4-argon",
    "gemini3.8flash": "gemini-3.8-flash",
    "luna5.6": "gpt-5.6-luna@1m",
}
TARGET_CONTEXT = 1000000
PREFERRED_EFFORT = "high"


def model_defaults(entry):
    """Prefer 1M context and high reasoning, but only where the catalog declares support."""
    window = entry.get('max_context_window') or entry.get('context_window')
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ValueError('模型目录缺少有效上下文上限；配置未修改。')
    levels = [x.get('effort') if isinstance(x, dict) else x
              for x in entry.get('supported_reasoning_levels') or []]
    effort = PREFERRED_EFFORT if PREFERRED_EFFORT in levels else entry.get('default_reasoning_level')
    return min(window, TARGET_CONTEXT), effort


def prefer_defaults(catalog):
    """Return a catalog copy whose per-model UI default uses high when supported."""
    result = json.loads(json.dumps(catalog))
    for entry in result.get('models', []):
        levels = [x.get('effort') if isinstance(x, dict) else x
                  for x in entry.get('supported_reasoning_levels') or []]
        if PREFERRED_EFFORT in levels:
            entry['default_reasoning_level'] = PREFERRED_EFFORT
    return result


def cursor_profile(root, model_name=None):
    """Resolve a short name against the installed gateway catalog, not guessed limits."""
    profile = (root / 'cursor.config.toml').read_text()
    selected = tomllib.loads(profile)
    if model_name:
        alias = model_name.lower()
        if alias == 'geimini4':
            alias = 'gemini4'
        if alias not in CURSOR_MODELS:
            raise ValueError('未知 Cursor 模型；支持: ' + ', '.join(CURSOR_MODELS))
        selected['model'] = 'cursor-plugin/' + CURSOR_MODELS[alias]
    catalog = json.loads(Path(selected['model_catalog_json']).read_text())
    entries = [m for m in catalog['models'] if m['slug'] == selected['model']]
    if len(entries) != 1:
        raise ValueError('本地模型目录未包含唯一的目标模型；配置未修改: ' + selected['model'])
    window, effort = model_defaults(entries[0])
    selected['model_context_window'] = window
    selected['model_auto_compact_token_limit'] = min(512000, window * 3 // 4)
    if effort:
        selected['model_reasoning_effort'] = effort
    else:
        selected.pop('model_reasoning_effort', None)
    return '\n'.join(f'{key} = {json.dumps(value, ensure_ascii=False)}'
                     for key, value in selected.items()) + '\n'


def split_tables(text):
    match = HEADER.search(text)
    return (text[:match.start()], text[match.start():]) if match else (text, "")


def render(current, profile):
    before = tomllib.loads(current)
    selected = tomllib.loads(profile)
    if set(selected) - KEYS - {"model_providers"}:
        raise ValueError("预设包含非模型配置，拒绝切换")
    if 'model' in selected and not selected['model']:
        raise ValueError("预设缺少 model")
    if current.count(BEGIN) != current.count(END) or current.count(BEGIN) > 1:
        raise ValueError("模型配置标记异常，拒绝修改")
    root, tables = split_tables(BLOCK.sub("", current))
    profile_root, profile_tables = split_tables(profile)
    keep = []
    for line in root.splitlines(keepends=True):
        match = SETTING.match(line)
        if not match or match.group(1) not in KEYS:
            keep.append(line)
    result = profile_root.rstrip() + "\n" + "".join(keep).lstrip("\n") + tables
    if profile_tables.strip():
        result = result.rstrip("\n") + "\n" + BEGIN + "\n" + profile_tables.rstrip() + "\n" + END + "\n"
    after = tomllib.loads(result)
    for k in (set(before) | set(after)) - KEYS - {"model_providers"}:
        if before.get(k) != after.get(k):
            raise ValueError("非模型配置发生变化，拒绝写入: " + k)
    return result


def switch(root, name, profile_text=None):
    config = root / "config.toml"
    profile = root / (name + ".config.toml")
    if config.is_symlink():
        raise ValueError("config.toml 是符号链接，请先人工确认目标")
    with (root / ".model-switch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = config.read_bytes()
        candidate = render(old.decode(), profile.read_text() if profile_text is None else profile_text)
        if tomllib.loads(candidate) == tomllib.loads(old.decode()):
            return None
        backups = root / "model-switch-backups"
        backups.mkdir(mode=0o700, exist_ok=True)
        fd, backup_name = tempfile.mkstemp(prefix="config-before-" + name + "-", suffix=".toml", dir=backups)
        os.close(fd)
        shutil.copyfile(config, backup_name)
        fd, staging = tempfile.mkstemp(prefix=".model-switch-", dir=root)
        try:
            with os.fdopen(fd, "w") as out:
                out.write(candidate)
                out.flush()
                os.fsync(out.fileno())
            if config.read_bytes() != old:
                raise ValueError("配置被其他进程修改，已取消切换")
            os.replace(staging, config)
        finally:
            if os.path.exists(staging):
                os.unlink(staging)
        return backup_name


def server_owner(root):
    """Resolve the owner of this config home's socket, never match all Codex PIDs."""
    socket = (root / "app-server-control/app-server-control.sock").resolve()
    inodes = set()
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[7] == str(socket):
            inodes.add('socket:[' + fields[6] + ']')
    owners = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != os.getuid():
                continue
            argv = [os.fsdecode(a) for a in (p / 'cmdline').read_bytes().split(b'\0') if a]
            if not argv or Path(argv[0]).name != 'codex' or 'app-server' not in argv or 'proxy' in argv:
                continue
            if any(os.readlink(fd) in inodes for fd in (p / 'fd').iterdir()):
                owners.append((int(p.name), argv))
        except (OSError, ProcessLookupError):
            continue
    if len(owners) != 1:
        raise ValueError('无法唯一定位当前配置的后端进程，未停止任何进程；请先在 UI 连接远端。')
    return owners[0]


def restart_plan(root):
    if sys.platform != 'linux':
        print('非 Linux 平台：仅更新配置；已运行的桌面 App 请手动退出并重开。')
        return None
    socket_path = str((root / 'app-server-control/app-server-control.sock').resolve())
    if not any(line.split()[-1:] == [socket_path] for line in Path('/proc/net/unix').read_text().splitlines()[1:]):
        return None  # A new/CLI-only machine has no app-server to restart.
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('此 Python/内核不支持安全 pidfd 重启；可用 --no-restart 后手动重开。')
    pid, argv = server_owner(root)
    p = Path('/proc') / str(pid)
    # A command-line model/profile override would defeat a config-file switch.
    if any(a in ('-m', '--model', '-p', '--profile') or a.startswith(('model', 'profile=', 'openai_base_url=')) for a in argv[1:]):
        raise ValueError('后端启动参数覆盖了模型配置，未修改或停止后端。')
    env = dict(os.fsdecode(x).split('=', 1) for x in (p / 'environ').read_bytes().split(b'\0') if b'=' in x)
    process_home = Path(env.get('CODEX_HOME', str(Path(env.get('HOME', str(Path.home()))) / '.codex'))).resolve()
    if process_home != root.resolve():
        raise ValueError('后端配置目录与目标不一致，拒绝重启。')
    return {'pid': pid, 'argv': argv, 'env': env, 'cwd': str((p / 'cwd').resolve()),
            'exe': str((p / 'exe').resolve()), 'start': (p / 'stat').read_text().rsplit(')', 1)[1].split()[19]}


def restart_backend(root, plan):
    pid = plan['pid']
    p = Path('/proc') / str(pid)
    if server_owner(root)[0] != pid or (p / 'stat').read_text().rsplit(')', 1)[1].split()[19] != plan['start']:
        raise ValueError('后端进程已变化，取消重启。配置已切换，可重新运行命令。')
    version = subprocess.run(['codex', 'app-server', 'daemon', 'version'], capture_output=True, text=True, timeout=10, check=True)
    managed = json.loads(version.stdout).get('managedCodexPath')
    if managed and str(Path(managed).resolve()) == plan['exe']:
        subprocess.run(['codex', 'app-server', 'daemon', 'restart'], check=True, timeout=45)
    else:
        # pidfd binds the signal to the process, protecting against PID reuse.
        fd = os.pidfd_open(pid)
        try:
            if (p / 'stat').read_text().rsplit(')', 1)[1].split()[19] != plan['start']:
                raise ValueError('后端进程已变化，取消重启。')
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            import select
            if not select.select([fd], [], [], 15)[0]:
                raise ValueError('后端在 15 秒内未退出，未强制终止；配置已切换。')
        finally:
            os.close(fd)
        # The desktop may have already started a replacement after disconnecting.
        try:
            server_owner(root)
        except ValueError:
            log = root / 'app-server-control/model-switch-restart.log'
            log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(log_fd, 'ab') as out:
                subprocess.Popen(plan['argv'], cwd=plan['cwd'], env=plan['env'],
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            new_pid, _ = server_owner(root)
            if new_pid != pid:
                check = subprocess.run(['codex', 'app-server', 'daemon', 'version'], capture_output=True, text=True, timeout=3)
                if check.returncode == 0 and json.loads(check.stdout).get('status') == 'running':
                    print('后端已重启并可连接。请在 UI 新建任务使用所选模型；若 UI 未自动恢复，点一次重新连接。')
                    return
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.5)
    raise ValueError('配置已切换，但后端就绪检查超时；请查看 app-server-control/model-switch-restart.log。')


def prepare_cursor(root, dry_run=False, profile_text=None):
    """Check the local gateway and exact model before any Codex config write."""
    profile = tomllib.loads((root / 'cursor.config.toml').read_text() if profile_text is None else profile_text)
    model = profile['model']
    if not model.startswith('cursor-plugin/'):
        raise ValueError('Cursor 预设必须指向已安装的 cursor-plugin 模型。')
    current = tomllib.loads((root / 'config.toml').read_text())
    state = load_state()
    provider = current.get('model_providers', {}).get(PROVIDER, {})
    if provider.get('base_url') != gateway_url(state) + '/v1' or provider.get('wire_api') != 'responses':
        raise ValueError('magpie provider 未注册或地址不匹配；配置未修改。')
    if not cursor_login_ok(state):
        raise ValueError('Cursor 登录检查未通过；请先运行 agent status，配置未修改。')
    last = None
    for _ in range(1 if dry_run else 10):
        try:
            models = gateway_ready(state, start=not dry_run)
            offered = next((item for item in models.get('data', []) if item['id'] == model), None)
            if offered is None:
                raise ValueError('magpie 未提供预设模型 ' + model + '；配置未修改。')
            if offered.get('context_window', 0) < profile.get('model_context_window', 0):
                raise ValueError('网关声明的上下文小于目标配置；配置未修改。')
            return
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and not isinstance(exc.__cause__, OSError):
                raise
            last = exc
            if not dry_run:
                time.sleep(0.5)
    raise ValueError('magpie 网关不可用；配置未修改。' + str(last))


def main():
    parser = argparse.ArgumentParser(description="模型快捷切换并重启后端；会断开该后端上的运行中会话。")
    parser.add_argument("tool", choices=["codex", "cursor"])
    parser.add_argument("preset", nargs="?", default="status", help="codex: openai / cursor / original / status")
    parser.add_argument("model", nargs="?", help="cursor 模型: " + ', '.join(CURSOR_MODELS) + "；省略使用默认 Opus 5.5")
    parser.add_argument('--no-restart', action='store_true', help='只切换配置，不重启')
    parser.add_argument('--dry-run', action='store_true', help='仅检查配置与重启目标，不修改、不重启')
    args = parser.parse_args()
    if args.tool == "cursor":
        parser.error("在 Codex UI 使用 Cursor 模型请运行 model-switch codex cursor；本命令不修改 Cursor CLI 的模型。")
    if args.preset not in {"status", "openai", "original", "cursor"}:
        parser.error("Codex 预设仅支持 openai、cursor、original、status")
    if args.model and args.preset != 'cursor':
        parser.error('模型名参数仅适用于 model-switch codex cursor {模型名}')
    state = load_state()
    if not state.get('configured'):
        raise ValueError('安装尚未完成（可能等待登录或网关）；请重新执行 install。')
    root = Path(state['codex_dir'])
    os.environ.update(tool_env(state))
    if os.environ.get('CODEX_HOME') and Path(os.environ['CODEX_HOME']).resolve() != root.resolve():
        raise ValueError('当前 CODEX_HOME 与安装目标不同，拒绝切换。')
    if args.preset != "status":
        with (root / '.model-switch-restart.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            profile = (cursor_profile(root, args.model) if args.preset == 'cursor'
                       else (root / (args.preset + '.config.toml')).read_text())
            render((root / 'config.toml').read_text(), profile)
            plan = None if args.no_restart else restart_plan(root)
            if args.preset == 'cursor':
                prepare_cursor(root, args.dry_run, profile)
            if args.dry_run:
                print('预检查通过；目标模型: ' + tomllib.loads(profile).get('model', 'Codex 默认') + ('；后端 PID: ' + str(plan['pid']) if plan else '；不重启'))
                return
            print('切换并重启将断开该后端上的会话。' if plan else '仅切换配置。', flush=True)
            backup = switch(root, args.preset, profile)
            print("已切换配置。备份: " + backup if backup else "配置已是该预设，无需修改。", flush=True)
            if plan:
                restart_backend(root, plan)
            elif not args.no_restart:
                print('配置已更新；无 Linux 后端需要重启。下次 CLI 启动生效；桌面 App 请重开并新建任务。')
    current = tomllib.loads((root / "config.toml").read_text())
    print("配置中的模型: " + current.get("model", "默认") + "；provider: " + current.get("model_provider", "openai"))
    print('上下文上限: ' + str(current.get('model_context_window', '模型默认')) +
          '；自动压缩阈值: ' + str(current.get('model_auto_compact_token_limit', '模型默认')))
    if args.preset != "status":
        if args.no_restart:
            print("未重启后端；桌面端需要重新加载配置后新建任务。")
        print("切回 OpenAI: model-switch codex openai")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("切换失败: " + str(exc), file=sys.stderr)
        sys.exit(1)
