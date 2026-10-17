import argparse
import hashlib
import io
import json
import os
import plistlib
import socket
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import kit
import model_switch as switch


class KitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kit test ')
        self.base = Path(self.tmp.name)
        self.prefix = self.base / 'installation space'
        self.root = self.base / 'codex space'
        self.state = {'prefix': str(self.prefix), 'codex_dir': str(self.root), 'port': 3437,
                      'python': sys.executable, 'agent': '/fake/agent', 'codex': '/fake/codex',
                      'magpie': '/fake/magpie', 'configured': True, 'service': 'none'}
        self.catalog = {'models': [{'slug': 'cursor-plugin/' + v, 'context_window': 272000,
                                   'max_context_window': 1000000, 'default_reasoning_level': 'low',
                                   'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium', 'high')]}
                                  for v in switch.CURSOR_MODELS.values()]}
        self.models = {'data': [{'id': m['slug'], 'context_window': 1000000} for m in self.catalog['models']]}

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self):
        return kit.configure(self.state, self.catalog, self.models, 'opus5.5')

    def test_supported_platforms(self):
        for system, machine, expected in [('Linux','x86_64','linux-x64'), ('Linux','aarch64','linux-arm64'),
                                          ('Darwin','arm64','darwin-arm64'), ('Darwin','x86_64','darwin-x64')]:
            self.assertEqual(kit.platform_key(system, machine), expected)
        with self.assertRaises(ValueError):
            kit.platform_key('Windows', 'AMD64')

    def test_checksum(self):
        path = self.base / 'asset'
        path.write_bytes(b'known')
        kit.verify(path, {'sha256': hashlib.sha256(b'known').hexdigest()})
        with self.assertRaisesRegex(ValueError, '校验失败'):
            kit.verify(path, {'sha256': '0' * 64})

    def test_archive_traversal_and_symlink(self):
        dest = self.base / 'extract'
        dest.mkdir()
        for name, link in [('../escape', None), ('/absolute', None), ('safe', '../../escape')]:
            archive = self.base / 'archive.tar'
            with tarfile.open(archive, 'w') as t:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = link
                t.addfile(member)
            with self.assertRaises(ValueError):
                kit.extract(archive, dest)
        self.assertEqual(list(dest.iterdir()), [])

    def test_fresh_configuration_keeps_builtin_default(self):
        self.assertEqual(len(self.configure()), len(switch.CURSOR_MODELS))
        current = tomllib.loads((self.root / 'config.toml').read_text())
        self.assertNotIn('model', current)
        self.assertIn(kit.PROVIDER, current['model_providers'])
        self.assertEqual((self.root / 'openai.config.toml').read_text(), '')
        self.assertEqual((self.root / 'config.toml').stat().st_mode & 0o777, 0o600)

    def test_all_aliases_roundtrip_and_preserve_mcp(self):
        self.root.mkdir()
        original = 'model="example-original"\nmodel_context_window=200000\n[mcp_servers.test]\nurl="https://example.invalid"\n'
        (self.root / 'config.toml').write_text(original)
        self.configure()
        initial = tomllib.loads((self.root / 'config.toml').read_text())
        for alias in switch.CURSOR_MODELS:
            selected = switch.cursor_profile(self.root, alias)
            values = tomllib.loads(selected)
            self.assertEqual(values['model_context_window'], 1000000)
            self.assertEqual(values['model_auto_compact_token_limit'], 512000)
            self.assertEqual(values['model_reasoning_effort'], 'high')
            backup = switch.switch(self.root, 'cursor', selected)
            self.assertEqual(Path(backup).stat().st_mode & 0o777, 0o600)
            current = tomllib.loads((self.root / 'config.toml').read_text())
            self.assertEqual(current['mcp_servers'], initial['mcp_servers'])
            self.assertIsNone(switch.switch(self.root, 'cursor', selected))
            switch.switch(self.root, 'original')
            self.assertEqual(tomllib.loads((self.root / 'config.toml').read_text()), initial)
        self.assertEqual(switch.cursor_profile(self.root, 'geimini4'), switch.cursor_profile(self.root, 'gemini4'))

    def test_defaults_prefer_high_and_1m_only_when_supported(self):
        self.configure()
        catalog = json.loads((self.root / 'model-switch-models.json').read_text())
        self.assertEqual({m['default_reasoning_level'] for m in catalog['models']}, {'high'})
        self.assertEqual(tomllib.loads((self.root / 'cursor.config.toml').read_text())['model_reasoning_effort'], 'high')
        self.assertEqual(switch.model_defaults({'max_context_window': 2000000,
                                                'supported_reasoning_levels': ['high']}), (1000000, 'high'))
        self.assertEqual(switch.model_defaults({'context_window': 256000, 'default_reasoning_level': 'low',
                                                'supported_reasoning_levels': [{'effort': 'low'}]}),
                         (256000, 'low'))
        self.assertEqual(switch.model_defaults({'context_window': 128000}), (128000, None))
        self.assertEqual(switch.prefer_defaults({'models': [{'default_reasoning_level': 'low',
                                                             'supported_reasoning_levels': ['low']}]}),
                         {'models': [{'default_reasoning_level': 'low', 'supported_reasoning_levels': ['low']}]})

    def test_reinstall_preserves_original_snapshot(self):
        self.root.mkdir()
        (self.root / 'config.toml').write_text('model="my-original"\n')
        self.configure()
        before = (self.root / 'original.config.toml').read_bytes()
        backup = (self.prefix / 'backups/before-first-config/config.toml').read_bytes()
        switch.switch(self.root, 'cursor', switch.cursor_profile(self.root))
        self.configure()
        self.assertEqual((self.root / 'original.config.toml').read_bytes(), before)
        self.assertEqual((self.prefix / 'backups/before-first-config/config.toml').read_bytes(), backup)
        self.assertEqual(tomllib.loads((self.root / 'config.toml').read_text())['model_provider'], kit.PROVIDER)

    def test_missing_model_or_bad_window_no_config_changes(self):
        with self.assertRaisesRegex(ValueError, '默认模型'):
            kit.configure(self.state, self.catalog, {'data': []}, 'opus5.5')
        self.assertFalse(self.root.exists())
        self.catalog['models'][0]['max_context_window'] = 2000000
        with self.assertRaisesRegex(ValueError, '上下文'):
            self.configure()
        self.assertFalse(self.root.exists())

    def test_provider_off_skips_missing_provider_only(self):
        script = self.base / 'fake-magpie'
        script.write_text('#!/bin/sh\nif [ "$3" = missing ]; then echo \'magpie: no provider "missing"\' >&2; exit 1; fi\n'
                          'if [ "$3" = broken ]; then echo boom >&2; exit 1; fi\nexit 0\n')
        script.chmod(0o755)
        kit.provider_off(script, 'present', None)
        kit.provider_off(script, 'missing', None)
        with self.assertRaisesRegex(ValueError, 'provider off broken'):
            kit.provider_off(script, 'broken', None)

    def test_provider_conflict_no_write(self):
        self.root.mkdir()
        original = '[model_providers.' + kit.PROVIDER + ']\nbase_url="https://example.invalid"\n'
        (self.root / 'config.toml').write_text(original)
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.configure()
        self.assertEqual((self.root / 'config.toml').read_text(), original)

    def test_config_symlink_rejected(self):
        self.root.mkdir()
        victim = self.base / 'keep.toml'
        victim.write_text('model="keep"\n')
        (self.root / 'config.toml').symlink_to(victim)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.configure()
        self.assertEqual(victim.read_text(), 'model="keep"\n')

    def test_profile_override_rejected(self):
        self.root.mkdir()
        (self.root / 'config.toml').write_text('profile="company"\n')
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.configure()

    def test_config_transaction_rollback(self):
        self.root.mkdir()
        original = 'model="keep"\n'
        (self.root / 'config.toml').write_text(original)
        real = kit.atomic
        def fail_one(path, content, mode=0o600):
            if path == self.root / 'cursor.config.toml':
                raise OSError('simulated disk failure')
            return real(path, content, mode)
        with patch.object(kit, 'atomic', side_effect=fail_one), self.assertRaises(OSError):
            self.configure()
        self.assertEqual((self.root / 'config.toml').read_text(), original)
        self.assertFalse((self.root / 'model-switch-models.json').exists())

    def test_wrappers_preserve_existing_commands_and_quote_spaces(self):
        bindir = self.base / 'user bin'
        bindir.mkdir()
        (bindir / 'model-switch').write_text('keep existing')
        kit.install_wrappers(self.state, bindir)
        self.assertEqual((bindir / 'model-switch').read_text(), 'keep existing')
        self.assertTrue((bindir / 'agent').is_symlink())
        wrapper = self.prefix / 'bin/model-switch'
        self.assertEqual(subprocess.run(['sh', '-n', str(wrapper)]).returncode, 0)
        self.assertIn("'", wrapper.read_text())

    def test_login_requires_success_and_authenticated_status(self):
        for rc, text, expected in [(0, 'Logged in', True), (1, 'Logged in', False), (0, 'Not logged in', False)]:
            with patch.object(kit.subprocess, 'run', return_value=subprocess.CompletedProcess([], rc, text, '')):
                self.assertEqual(kit.cursor_login_ok(self.state), expected)

    def test_gateway_preflight_checks_selected_model_and_window(self):
        self.configure()
        profile = switch.cursor_profile(self.root, 'sonnet5.5')
        for result, error in [({'data': []}, '未提供'),
                              ({'data': [{'id': 'cursor-plugin/claude-sonnet-5-5@1m', 'context_window': 200000}]}, '上下文')]:
            before = (self.root / 'config.toml').read_bytes()
            with patch.object(switch, 'load_state', return_value=self.state), \
                    patch.object(switch, 'cursor_login_ok', return_value=True), \
                    patch.object(switch, 'gateway_ready', return_value=result), self.assertRaisesRegex(ValueError, error):
                switch.prepare_cursor(self.root, True, profile)
            self.assertEqual((self.root / 'config.toml').read_bytes(), before)

    def test_no_backend_is_valid_first_install(self):
        with patch.object(switch.sys, 'platform', 'linux'):
            self.assertIsNone(switch.restart_plan(self.root))

    def args(self):
        return argparse.Namespace(prefix=str(self.prefix), codex_dir=str(self.root),
                                  vendor_dir=str(self.base / 'vendor'), check=False, download_only=False,
                                  port=3437, bin_dir=str(self.base / 'bin'), service='none', model='opus5.5')

    def test_login_block_does_not_touch_codex(self):
        with patch.object(kit, 'install_tool', return_value=Path('/fake/tool')), \
                patch.object(kit.socket, 'socket'), \
                patch.object(kit, 'cursor_login_ok', return_value=False), \
                patch.object(kit, 'configure') as configure:
            self.assertEqual(kit.setup(self.args()), 3)
            self.assertFalse(self.root.exists())
            configure.assert_not_called()
            self.assertFalse(json.loads((self.prefix / 'state.json').read_text()).get('configured', False))

    def test_check_does_not_create_installation(self):
        args = self.args()
        args.check = True
        with patch.object(kit, 'user_systemd', return_value=False):
            kit.setup(args)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.root.exists())

    def test_corrupt_vendor_asset_not_downloaded_or_executed(self):
        vendor = self.base / 'vendor'
        vendor.mkdir()
        (vendor / 'bad').write_bytes(b'bad')
        with patch.object(kit, 'run') as run, self.assertRaisesRegex(ValueError, '校验失败'):
            kit.download({'file': 'bad', 'sha256': '0' * 64}, self.base / 'cache', vendor)
        run.assert_not_called()

    def test_systemd_service_is_scoped_and_refuses_other_install(self):
        with patch.object(kit, 'user_systemd', return_value=True), \
                patch.object(kit.Path, 'home', return_value=self.base), patch.object(kit, 'run'):
            self.assertEqual(kit.install_service(self.state, 'auto'), 'systemd')
            service = self.base / '.config/systemd/user' / kit.SERVICE
            self.assertIn('UMask=0077', service.read_text())
            self.assertIn('ExecStart="', service.read_text())
            other = dict(self.state, prefix=str(self.base / 'other'))
            with self.assertRaisesRegex(ValueError, '占用'):
                kit.install_service(other, 'auto')

    def test_launchd_template(self):
        with patch.object(kit, 'user_systemd', return_value=False), \
                patch.object(kit.sys, 'platform', 'darwin'), \
                patch.object(kit.Path, 'home', return_value=self.base), \
                patch.object(kit, 'run'), \
                patch.object(kit.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(kit.install_service(self.state, 'auto'), 'launchd')
            path = self.base / 'Library/LaunchAgents' / (kit.LABEL + '.plist')
            value = plistlib.loads(path.read_bytes())
            self.assertEqual(value['ProgramArguments'], [str(self.prefix / 'bin/model-switch-gateway')])

    def test_cursor_profile_with_mcp_is_not_overwritten(self):
        self.root.mkdir()
        (self.root / 'cursor.config.toml').write_text('model="mine"\n[mcp_servers.mine]\ncommand="example"\n')
        with self.assertRaisesRegex(ValueError, '非模型配置'):
            self.configure()
        self.assertFalse((self.root / 'config.toml').exists())

    def make_cached_tool(self, executable=True):
        target = self.prefix / 'tools/codex-test'
        target.mkdir(parents=True)
        binary = target / 'codex'
        binary.write_text('#!/bin/sh\nprintf "codex-cli test\\n"\n')
        binary.chmod(0o700 if executable else 0o600)
        (target / '.installed.json').write_text(json.dumps({'spec': {}, 'executable': 'codex'}))
        return binary

    def test_cached_tool_must_be_executable(self):
        self.make_cached_tool(executable=False)
        with patch.object(kit, 'download') as download, self.assertRaisesRegex(ValueError, '不可执行'):
            kit.install_tool('codex', {}, self.prefix, self.base / 'cache', self.base / 'vendor', 'test')
        download.assert_not_called()

    def test_cached_healthy_tool_is_reused_without_download(self):
        binary = self.make_cached_tool()
        with patch.object(kit, 'download') as download:
            self.assertEqual(kit.install_tool('codex', {}, self.prefix, self.base / 'cache', self.base / 'vendor', 'test'), binary)
        download.assert_not_called()

    def test_cached_wrong_version_is_rejected(self):
        self.make_cached_tool().write_text('#!/bin/sh\nprintf "codex-cli other\\n"\n')
        with self.assertRaisesRegex(ValueError, '版本不一致'):
            kit.cached_tool('codex', {}, self.prefix, 'test')

    def test_cached_broken_version_command_is_rejected(self):
        self.make_cached_tool().write_text('#!/bin/sh\nexit 7\n')
        with self.assertRaisesRegex(ValueError, '退出码 7'):
            kit.cached_tool('codex', {}, self.prefix, 'test')

    def test_cached_version_check_has_timeout(self):
        binary = self.make_cached_tool()
        with patch.object(kit, 'run', side_effect=subprocess.TimeoutExpired('codex', 30)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                kit.cached_tool('codex', {}, self.prefix, 'test')
            run.assert_called_once_with([binary, '--version'], timeout=30)

    def test_preflight_occupied_port_blocks_check_and_install_without_writes(self):
        with socket.socket() as listener, patch.object(kit, 'install_tool') as install:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            args = self.args()
            args.port = listener.getsockname()[1]
            for check in (True, False):
                args.check = check
                with self.assertRaisesRegex(ValueError, '端口已占用'):
                    kit.setup(args)
            install.assert_not_called()
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.root.exists())

    def test_preflight_config_conflict_precedes_install(self):
        self.root.mkdir()
        original = '[model_providers.' + kit.PROVIDER + ']\nbase_url="https://example.invalid"\n'
        (self.root / 'config.toml').write_text(original)
        with patch.object(kit, 'install_tool') as install:
            for check in (True, False):
                args = self.args()
                args.check = check
                with self.assertRaisesRegex(ValueError, '冲突'):
                    kit.setup(args)
            install.assert_not_called()
        self.assertFalse(self.prefix.exists())
        self.assertEqual((self.root / 'config.toml').read_text(), original)

    def test_preflight_service_conflict_precedes_install(self):
        unit = self.base / '.config/systemd/user' / kit.SERVICE
        unit.parent.mkdir(parents=True)
        unit.write_text('# belongs to someone else\n')
        with patch.object(kit, 'user_systemd', return_value=True), \
                patch.object(kit.Path, 'home', return_value=self.base), \
                patch.object(kit, 'install_tool') as install:
            for check in (True, False):
                args = self.args()
                args.service = 'auto'
                args.check = check
                with self.assertRaisesRegex(ValueError, '占用服务名称'):
                    kit.setup(args)
            install.assert_not_called()
        self.assertFalse(self.prefix.exists())
        self.assertEqual(unit.read_text(), '# belongs to someone else\n')

    def test_preflight_requires_service_before_install(self):
        args = self.args()
        args.service = 'auto'
        with patch.object(kit, 'user_systemd', return_value=False), \
                patch.object(kit.sys, 'platform', 'linux'), \
                patch.object(kit, 'install_tool') as install, self.assertRaisesRegex(ValueError, '--service none'):
            kit.setup(args)
        install.assert_not_called()
        self.assertFalse(self.prefix.exists())

    def test_download_only_check_does_not_require_gateway_or_codex_config(self):
        args = self.args()
        args.check = args.download_only = True
        self.root.mkdir()
        (self.root / 'config.toml').write_text('profile="company"\n')
        with patch.object(kit, 'user_systemd', return_value=False):
            self.assertEqual(kit.setup(args), 0)
        self.assertFalse(self.prefix.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
