import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

import image_provider as provider


def png():
    buf = io.BytesIO(); Image.new('RGBA',(60,40),(120,60,200,140)).save(buf,'PNG')
    return buf.getvalue()


class ImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def test_daily_reservations_are_atomic_under_concurrency(self):
        with patch.dict(os.environ,{'IMAGE_USAGE_DB':str(self.root/'usage.sqlite3')}), patch.object(provider,'DAILY_LIMIT',10):
            def reserve(_):
                try: provider._reserve(); return True
                except ValueError: return False
            with ThreadPoolExecutor(max_workers=6) as pool:
                self.assertEqual(sum(pool.map(reserve,range(24))),10)

    def test_missing_credentials_never_contacts_provider(self):
        with patch.dict(os.environ,{'CLOUDFLARE_ACCOUNT_ID':'','CLOUDFLARE_API_TOKEN':''}), patch.object(provider.requests,'post') as post:
            with self.assertRaisesRegex(ValueError,'needs CLOUDFLARE'):
                provider.generate(self.root/'out.png','test')
            post.assert_not_called()

    def test_cloudflare_multipart_generation_and_reference_image(self):
        body = json.dumps({'success':True,'result':{'image':base64.b64encode(png()).decode()}}).encode()
        class Response:
            status_code = 200
            headers = {'Content-Type':'application/json'}
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def raise_for_status(self): pass
            def iter_content(self,size): yield body
        with patch.dict(os.environ,{'CLOUDFLARE_ACCOUNT_ID':'a'*32,'CLOUDFLARE_API_TOKEN':'test-token','IMAGE_USAGE_DB':str(self.root/'usage.sqlite3')}), patch.object(provider.requests,'post',return_value=Response()) as post:
            provider.generate(self.root/'out.png',{'prompt':'Paint this purple','aspect':'square'},[png()])
            kwargs = post.call_args.kwargs
            self.assertEqual(kwargs['files']['width'],(None,'1024'))
            self.assertIn('input_image_0',kwargs['files'])
            self.assertNotIn('json',kwargs)
            self.assertIn('/flux-2-klein-4b',post.call_args.args[0])
            with Image.open(self.root/'out.png') as im: im.verify()

    def test_rate_limit_does_not_retry_or_use_paid_fallback(self):
        class Response:
            status_code = 429
            def __enter__(self): return self
            def __exit__(self,*args): pass
        with patch.dict(os.environ,{'CLOUDFLARE_ACCOUNT_ID':'a'*32,'CLOUDFLARE_API_TOKEN':'test-token','IMAGE_USAGE_DB':str(self.root/'usage.sqlite3')}), patch.object(provider.requests,'post',return_value=Response()) as post:
            with self.assertRaisesRegex(ValueError,'allowance'):
                provider.generate(self.root/'out.png','test')
            self.assertEqual(post.call_count,1)
            self.assertFalse((self.root/'out.png').exists())


if __name__ == '__main__': unittest.main()
