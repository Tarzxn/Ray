"""Cloudflare FLUX.2 klein 4B; fixed free-tier-sized outputs, no paid fallback."""
import base64
from contextlib import closing
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import sqlite3

from PIL import Image, ImageOps
import requests

MODEL = '@cf/black-forest-labs/flux-2-klein-4b'
DAILY_LIMIT = 50
ASPECTS = {'square':(1024,1024), 'portrait':(768,1024), 'landscape':(1024,768)}


def configured():
    return bool(os.environ.get('CLOUDFLARE_ACCOUNT_ID','').strip() and os.environ.get('CLOUDFLARE_API_TOKEN','').strip())


def _reserve():
    path = Path(os.environ.get('IMAGE_USAGE_DB', 'data/image-usage.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with closing(sqlite3.connect(path, timeout=10)) as db:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO usage VALUES (?,0)',(day,))
            count = db.execute('SELECT requests FROM usage WHERE day=?',(day,)).fetchone()[0]
            if count >= DAILY_LIMIT:
                raise ValueError('The shared 50-image daily limit is reached. It resets at 00:00 UTC; no paid fallback is used.')
            db.execute('UPDATE usage SET requests=requests+1 WHERE day=?',(day,))
            db.execute('DELETE FROM usage WHERE day < ?',((dt.date.fromisoformat(day)-dt.timedelta(days=7)).isoformat(),))


def generate(path, content, references=()):
    if not configured():
        raise ValueError('Image generation needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN on Render. Use a Workers Free account; see SETUP.md.')
    account = os.environ['CLOUDFLARE_ACCOUNT_ID'].strip()
    if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
        raise ValueError('CLOUDFLARE_ACCOUNT_ID must be the 32-character account ID')
    if not isinstance(content, dict):
        try:
            parsed = json.loads(content)
            content = parsed if isinstance(parsed, dict) else {'prompt':str(content)}
        except (TypeError, ValueError):
            content = {'prompt':str(content)}
    prompt = str(content.get('prompt','')).strip()[:3000]
    if not prompt:
        raise ValueError('Image prompt is empty')
    width, height = ASPECTS.get(str(content.get('aspect','square')), ASPECTS['square'])
    # requests files=(None,value) forces multipart even for text-only calls.
    fields = {'prompt':(None,prompt), 'width':(None,str(width)), 'height':(None,str(height))}
    for i, raw in enumerate(references[:4]):
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im).convert('RGB')
            im.thumbnail((504,504))
            buf = io.BytesIO(); im.save(buf,'JPEG',quality=90)
        fields[f'input_image_{i}'] = (f'reference-{i}.jpg',buf.getvalue(),'image/jpeg')
    _reserve()
    # Do not retry failed generation automatically: the upstream may have charged
    # compute even if the response was lost. No alternate provider is contacted.
    try:
        with requests.post(f'https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}',
                           headers={'Authorization':'Bearer '+os.environ['CLOUDFLARE_API_TOKEN'].strip()},
                           files=fields, timeout=(10,100), stream=True) as response:
            if response.status_code == 429:
                raise ValueError('Cloudflare free allowance or rate limit reached. Try later; no paid fallback is used.')
            if response.status_code in (401,403):
                raise ValueError('Cloudflare rejected the credentials or model access. Check the account ID and Workers AI token permission.')
            response.raise_for_status()
            chunks, length = [], 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > 16*1024*1024:
                    raise ValueError('Image response exceeds the 16 MB limit')
                chunks.append(chunk)
            data = b''.join(chunks)
            if 'json' in response.headers.get('Content-Type','').lower():
                result = json.loads(data)
                if result.get('success') is False:
                    raise ValueError('Cloudflare did not produce an image. Check its dashboard for details.')
                encoded = (result.get('result') or {}).get('image') or result.get('image')
                if not isinstance(encoded,str):
                    raise ValueError('Cloudflare returned no image')
                data = base64.b64decode(encoded,validate=True)
        with Image.open(io.BytesIO(data)) as im:
            if im.width*im.height > 4_000_000:
                raise ValueError('Generated image is unexpectedly large')
            im.load()
            ext = Path(path).suffix.lower()
            fmt = {'.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP','.gif':'GIF'}.get(ext,'PNG')
            im.convert('RGB').save(path,fmt)
    except requests.RequestException as error:
        raise ValueError('Cloudflare image request failed. Check service status and credentials; this attempt counts toward the local daily cap.') from error
