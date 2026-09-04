import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import serve


class DeploymentTests(unittest.TestCase):
    def test_webhook_secret_fingerprint_is_opt_in(self):
        with patch('builtins.print') as output:
            result = serve.log_webhook_secret_fingerprint(
                {'POSTBODE_WEBHOOK_SECRET': 'secret-value'})
        self.assertIsNone(result)
        output.assert_not_called()

    def test_webhook_secret_fingerprint_hides_secret(self):
        secret = 'secret-value'
        with patch('builtins.print') as output:
            result = serve.log_webhook_secret_fingerprint({
                'LOG_WEBHOOK_SECRET_FINGERPRINT_ON_START': 'true',
                'POSTBODE_WEBHOOK_SECRET': secret,
            })
        self.assertEqual(result, {'length': 12, 'sha256_12': '31160254d129'})
        self.assertNotIn(secret, str(output.call_args))
        output.assert_called_once()

    def test_startup_connection_check_is_opt_in(self):
        checker = Mock(return_value={'openai': 'ok', 'google': 'ok'})
        self.assertIsNone(serve.run_startup_connection_check({}, checker))
        checker.assert_not_called()

    def test_startup_connection_check_runs_once(self):
        checker = Mock(return_value={'openai': 'ok', 'google': 'ok'})
        with patch('builtins.print') as output:
            result = serve.run_startup_connection_check(
                {'CHECK_CONNECTIONS_ON_START': 'true'}, checker)
        self.assertEqual(result, {'openai': 'ok', 'google': 'ok'})
        checker.assert_called_once_with()
        output.assert_called_once()

    def test_startup_connection_check_logs_only_safe_failure_metadata(self):
        secret = 'never-log-this-secret'
        error = RuntimeError(secret)
        with patch('builtins.print') as output:
            with self.assertRaises(RuntimeError):
                serve.run_startup_connection_check(
                    {'CHECK_CONNECTIONS_ON_START': 'true'}, Mock(side_effect=error))
        rendered = str(output.call_args)
        self.assertIn('RuntimeError', rendered)
        self.assertNotIn(secret, rendered)

    def test_startup_synthetic_pilot_requires_explicit_opt_in(self):
        pilot = Mock(return_value={'verified': True})
        self.assertIsNone(serve.run_startup_synthetic_pilot({}, pilot))
        pilot.assert_not_called()

    def test_startup_synthetic_pilot_runs_once(self):
        pilot = Mock(return_value={'verified': True})
        with patch('builtins.print'):
            result = serve.run_startup_synthetic_pilot(
                {'RUN_SYNTHETIC_PILOT_ON_START': 'true'}, pilot)
        self.assertEqual(result, {'verified': True})
        pilot.assert_called_once_with()

    def test_setup_does_not_start_worker(self):
        self.assertEqual([n for n,c in serve.commands({})], ['receiver'])
    def test_uses_platform_port(self):
        cmd = serve.commands({'PORT':'9123'})[0][1]
        self.assertIn('0.0.0.0:9123',cmd)
    def test_processing_cannot_start_without_credentials(self):
        with self.assertRaises(ValueError):
            serve.commands({'PROCESSING_ENABLED':'true'})
    def test_enabled_processing_includes_both_processes(self):
        env = dict.fromkeys(['OPENAI_API_KEY','POSTBODE_WEBHOOK_SECRET','POSTBODE_RECIPIENT_UUID',
                             'ALERT_EMAIL','GOOGLE_TOKEN_JSON'],'synthetic')
        env['PROCESSING_ENABLED']='true'
        self.assertEqual([n for n,c in serve.commands(env)],['receiver','worker'])
    def test_missing_railway_volume_blocks_startup(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ,{'DATA_DIR':d,'RAILWAY_ENVIRONMENT_ID':'test'},clear=True):
                with self.assertRaises(RuntimeError):
                    serve.prepare_volume()
    def test_child_failure_stops_other_process(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile=d+'/child.pid'
            long=[sys.executable,'-c',
                  "import os,time,pathlib;pathlib.Path("+repr(pidfile)+").write_text(str(os.getpid()));time.sleep(30)"]
            short=[sys.executable,'-c','import time;time.sleep(.3);raise SystemExit(1)']
            self.assertEqual(serve.supervise([('long',long),('failed',short)]),1)
            with open(pidfile) as f:pid=int(f.read())
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)


if __name__=='__main__':
    unittest.main()
