import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import require_sandbox
require_sandbox()
from tests.sandbox import PATH_ENV, make_environment, snapshot, under, verify_snapshot
import hub

ROOT = Path(__file__).resolve().parents[1]


class SandboxTest(unittest.TestCase):
    def run_code(self, code, env=None):
        return subprocess.run([sys.executable,'-B','-c',code],env=env or dict(os.environ),
                              capture_output=True,text=True,timeout=15)

    def test_home_expansion_and_import_time_constants_are_temporary(self):
        root=require_sandbox()
        self.assertEqual(Path.home(),Path(os.environ['HOME']))
        self.assertEqual(Path(os.path.expanduser('~')),Path.home())
        for name in PATH_ENV: self.assertTrue(under(os.environ[name],root),name)
        for path in (hub.DATA_DIR,hub.HUB_DIR,hub.REGISTRY_PATH,hub.BINDINGS_PATH,hub.STATE_DB_PATH):
            self.assertTrue(under(path,root),str(path))
        self.assertTrue(sys.dont_write_bytecode)
        self.assertNotIn('CC_SESSION_KEY',os.environ)
        self.assertNotIn('CC_RELAY_HUB_BIN',os.environ)

    def test_original_bootstrap_failure_path_writes_only_temporary_global_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            env=make_environment(folder,ROOT)
            code='''
import os,json,unittest
from pathlib import Path
import hub
from tests.test_bootstrap import BootstrapTest
# Reproduce T10's initially empty isolated hub: the mocked bootstrap fallback
# supplies bot-a and cmd_bootstrap still performs its real context-file writes.
hub.REGISTRY_PATH.unlink(); hub.BINDINGS_PATH.unlink()
suite=unittest.TestSuite(BootstrapTest(name) for name in [
    'test_returns_one_when_webhook_down','test_returns_zero_when_all_up'])
result=unittest.TextTestRunner().run(suite)
p=Path.home()/'.claude/CLAUDE.md'
assert result.wasSuccessful() and p.is_file() and 'bot-a' in p.read_text()
assert str(Path(os.environ['CC_RELAY_TEST_ROOT'])) in str(p)
print('BOOTSTRAP_REPRODUCED_IN_TEMP_HOME')
'''
            result=self.run_code(code,env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('BOOTSTRAP_REPRODUCED_IN_TEMP_HOME',result.stdout)

    def test_environment_overrides_inherited_live_paths_and_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=dict(os.environ,HOME='/not-a-test-home',CODEX_HOME='/not-a-test-codex',
                        CC_CONFIG='/not-a-test-config',CC_SESSION_KEY='never-forward-this',
                        CC_PROJECT='not-a-test-project',CC_RELAY_HUB_BIN='/not-a-test-launcher')
            env=make_environment(folder,ROOT,parent)
            require_sandbox(env)
            for name in PATH_ENV: self.assertTrue(under(env[name],folder),name)
            self.assertNotIn('CC_SESSION_KEY',env)
            self.assertNotIn('CC_PROJECT',env)
            self.assertNotIn('CC_RELAY_HUB_BIN',env)

    def test_fingerprint_guard_detects_content_mtime_and_new_files(self):
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'existing',Path(folder)/'missing'
            a.write_text('initial')
            before=snapshot([a,b])
            self.assertTrue(verify_snapshot(before)['ok'])
            a.write_text('changed');os.utime(a,ns=(a.stat().st_atime_ns,before[str(a)]['mtime_ns']))
            self.assertEqual(verify_snapshot(before)['changed'],[str(a)])
            a.write_text('initial');os.utime(a,ns=(a.stat().st_atime_ns,before[str(a)]['mtime_ns']+1))
            self.assertEqual(verify_snapshot(before)['changed'],[str(a)])
            b.write_text('created')
            self.assertEqual(set(verify_snapshot(before)['changed']),{str(a),str(b)})

    def test_write_guard_blocks_absolute_home_and_sqlite_escapes_before_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_home=Path(folder)/'fake-production';fake_home.mkdir()
            sentinel=fake_home/'sentinel';sentinel.write_text('keep')
            env=make_environment(Path(folder)/'child',ROOT,
                                 dict(os.environ,CC_RELAY_TEST_REAL_HOME=str(fake_home)))
            env['TEST_SENTINEL']=str(sentinel)
            code='''
import os,sqlite3,subprocess,sys
from pathlib import Path
p=Path(os.environ['TEST_SENTINEL'])
actions=[lambda:p.write_text('bad'),lambda:p.unlink(),lambda:sqlite3.connect(str(p)),
 lambda:subprocess.run([sys.executable,'-c','pass'],env=dict(os.environ,HOME=str(p.parent)))]
for action in actions:
 try: action()
 except (PermissionError,RuntimeError): pass
 else: raise AssertionError('unguarded write/child env')
assert p.read_text()=='keep'
print('BLOCKED_BEFORE_WRITE')
'''
            result=self.run_code(code,env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('BLOCKED_BEFORE_WRITE',result.stdout)
            self.assertEqual(sentinel.read_text(),'keep')

    def test_runner_returns_failure_when_protected_fingerprint_changes(self):
        # Simulate an external writer with a fake production HOME. No real
        # protected path is changed, even when testing the guard's failure path.
        spec=importlib.util.spec_from_file_location('sandbox_runner_test',ROOT/'tests/run_isolated.py')
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'fake-production';p=home/'.claude/CLAUDE.md'
            p.parent.mkdir(parents=True);p.write_text('initial')
            report=Path(folder)/'guard.json'
            def fake_worker(*args,**kwargs):
                p.write_text('changed externally')
                return subprocess.CompletedProcess(args,0)
            with patch.dict(os.environ,{'CC_RELAY_TEST_REAL_HOME':str(home)}), \
                 patch.object(runner.subprocess,'run',side_effect=fake_worker):
                self.assertEqual(runner.main(['--guard-report',str(report)]),1)
            result=json.loads(report.read_text())
            self.assertFalse(result['ok'])
            self.assertEqual(result['changed'],[str(p)])

    def test_unsafe_direct_test_entry_fails_before_application_import(self):
        result=self.run_code('''
import os,runpy
os.environ.pop('CC_RELAY_TEST_ROOT')
runpy.run_path(%r,run_name='__main__')
''' % str(ROOT/'tests/test_bootstrap.py'))
        self.assertNotEqual(result.returncode,0)
        self.assertIn('tests/run_isolated.py',result.stderr)


if __name__=='__main__': unittest.main()
