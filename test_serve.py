import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import serve


class DeploymentTests(unittest.TestCase):
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
