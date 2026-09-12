"""Local-only disposable preview with empty data, never the production database."""
import os
import tempfile
from wsgiref.simple_server import make_server
from unittest.mock import patch
import cockpit


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='cockpit-preview-') as folder:
        os.environ['DATA_DIR']=folder
        def app(environ,start):
            with patch('dashboard.signing_key',return_value=b'local-preview-only'):
                html=cockpit.page('preview-only')
            start('200 OK',[('Content-Type','text/html; charset=utf-8')])
            return [html.encode()]
        make_server('127.0.0.1',8765,app).serve_forever()
