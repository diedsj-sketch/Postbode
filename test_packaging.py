"""Exercise the files shipped to production, not the larger working directory."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

class PackagingTests(unittest.TestCase):
    def test_packaged_dashboard_renders(self):
        root=Path(__file__).resolve().parent
        for dockerfile in ('Dockerfile','Dockerfile.railway'):
            with self.subTest(dockerfile=dockerfile),tempfile.TemporaryDirectory() as folder:
                target=Path(folder)
                for line in (root/dockerfile).read_text().splitlines():
                    if line.startswith('COPY '):
                        for source in line.split()[1:-1]:
                            shutil.copy2(root/source,target/source)
                script="import cockpit; from mailroom import db; import payment_planner; c=db(); c.__enter__(); c.__exit__(None,None,None); html=cockpit.page('test'); assert 'Your next income cycle' in html"
                env=dict(os.environ,DATA_DIR=str(target/'data'),DASHBOARD_PASSWORD='test-only',PYTHONPATH='')
                result=subprocess.run([sys.executable,'-c',script],cwd=target,env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
