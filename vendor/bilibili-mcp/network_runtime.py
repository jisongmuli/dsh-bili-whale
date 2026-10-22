"""Normalize inherited CLI network settings before any HTTP client is built."""
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def configure(home=None):
    home = Path(home or Path(__file__).resolve().parent)
    # Node API initialization is not applicable to HTTPX or the native CLI.
    # Inheriting this flag into AGY can stall it before the first event.
    os.environ.pop('NODE_USE_ENV_PROXY',None)
    bypass = (os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or '').split(',')
    bypass += ['localhost', '127.0.0.1', '.bilibili.com', '.biliapi.net', '.hdslb.com',
               '.biliimg.com', '.bilivideo.com', 'api.deepseek.com']
    bypass = list(dict.fromkeys(s.strip() for s in bypass if s.strip() == '*' or
        re.fullmatch(r'\.?[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*(?::\d+)?', s.strip())))
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = ','.join(bypass)
    try:
        settings = json.loads((home/'network-settings.json').read_text(encoding='utf-8'))
        proxy = settings.get('proxy', {})
        address = proxy.get('https') or proxy.get('http') or ''
        parsed = urlsplit(address)
        if parsed.scheme in ('http', 'https') and parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
            os.environ.update(HTTP_PROXY=address, HTTPS_PROXY=address, http_proxy=address, https_proxy=address)
    except (ValueError, OSError):
        pass
    # On this machine Windows' default temp directory is inaccessible to pipe creation.
    target = home/'tmp'
    target.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(target), TMP=str(target))
    tempfile.tempdir = str(target)
