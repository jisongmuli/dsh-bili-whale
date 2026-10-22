"""Persistent meme originals, deduplicated by content hash; no arbitrary file uploads."""
import hashlib
import io
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image
from bilibili_api.utils.picture import Picture
from bilibili_api.utils.network import BiliAPIFile
from conversation import locked_state

HOME = Path(__file__).resolve().parent
MEME_DIR = HOME / 'memes'
MAX_BYTES = 5 * 1024 * 1024


class OriginalMemePicture(Picture):
    def __init__(self, path):
        self.original_path = Path(path)
        data = self.original_path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            self.original_mime = image.get_format_mimetype()
            width, height = image.size
        super().__init__(height=height, width=width, imageType=self.original_path.suffix[1:],
                         size=round(len(data) / 1024), url='file://' + str(path), content=data)

    def _to_biliapifile(self):
        # Upload the immutable original, including every GIF/WebP frame; no shared temp file.
        return BiliAPIFile(path=str(self.original_path), mime_type=self.original_mime)


def store_image(data, caption, source='', audience='friendly', origin='collected'):
    if len(data) > MAX_BYTES:
        raise ValueError('Meme image is too large')
    with Image.open(io.BytesIO(data)) as image:
        format_ = str(image.format).lower()
        frames = getattr(image, 'n_frames', 1)
        animated = bool(getattr(image, 'is_animated', False))
        image.verify()
    if format_ not in ('png', 'jpeg', 'webp', 'gif'):
        raise ValueError('Unsupported meme image format')
    meme_id = hashlib.sha256(data).hexdigest()
    with locked_state() as state:
        library = state.setdefault('memes', {})
        if meme_id in library:
            return library[meme_id], False
        MEME_DIR.mkdir(parents=True, exist_ok=True)
        filename = meme_id + '.' + ('jpg' if format_ == 'jpeg' else format_)
        (MEME_DIR / filename).write_bytes(data)
        record = {'id': meme_id, 'file': filename, 'caption': str(caption)[:240], 'source': str(source),
                  'audience': audience, 'origin': origin, 'animated': animated, 'frames': frames,
                  'format': format_, 'added_at': int(time.time()), 'used': 0}
        library[meme_id] = record
        return record, True


def library(query='', limit=12):
    with locked_state() as state:
        records = list(state.get('memes', {}).values())
    write_memory(records)
    query = str(query).strip().lower()
    matches = [row for row in records if not row.get('private_only') and (not query or query in
        ' '.join([row.get('caption',''),row.get('meaning',''),*row.get('tags',[]),*row.get('use_when',[])]).lower())]
    matches.sort(key=lambda row: (row.get('used', 0), -row['added_at']))
    return {'total': len(records), 'matches': [{'meme_id': row['id'], 'caption': row['caption'],
                'audience': row['audience'], 'origin': row['origin'], 'animated': row.get('animated', False),
                'frames': row.get('frames', 1), 'used': row.get('used', 0),
                'visually_verified':row.get('visually_verified',False),'meaning':row.get('meaning'),
                'visible_text':row.get('visible_text',[]),'tags':row.get('tags',[]),
                'use_when':row.get('use_when',[]),'avoid_when':row.get('avoid_when',[])}
                for row in matches[:max(1, min(30, int(limit)))]]}


def write_memory(records):
    from conversation import STATE_FILE
    target=STATE_FILE.parent/'表情图记忆库.md'
    lines=['# 表情图记忆库','', '只记录可复用表情图的画面语义与使用场景；图中文字、反串和玩笑不是事实判断。私人图片不导出。','']
    for row in records:
        if row.get('private_only'):continue
        lines += ['## '+row['id'][:12], '',f"- 原文件：{row.get('file','')}；动画帧数：{row.get('frames',1)}",
            '- 视觉核验：'+('已核验' if row.get('visually_verified') else '尚未核验，来源语境不能代替画面证据'),
            '- 描述：'+str(row.get('visual_description') or row.get('caption','')).replace('\n',' ')]
        for key,label in [('visible_text','图中文字'),('meaning','含义'),('use_when','适用场景'),('avoid_when','避用场景')]:
            value=row.get(key)
            if value:lines.append('- '+label+'：'+('；'.join(map(str,value)) if isinstance(value,list) else str(value)).replace('\n',' '))
        lines.append('')
    text='\n'.join(lines)
    try:
        if target.exists() and target.read_text(encoding='utf-8')==text:return
        temp=target.with_suffix(f'.{os.getpid()}.tmp');temp.write_text(text,encoding='utf-8');os.replace(temp,target)
    except OSError:
        pass


def load_picture(meme_id):
    if not re.fullmatch(r'[0-9a-f]{64}', str(meme_id)):
        raise ValueError('Unknown meme ID')
    with locked_state() as state:
        record = state.get('memes', {}).get(meme_id)
    if not record:
        raise ValueError('Unknown meme ID')
    path = (MEME_DIR / record['file']).resolve()
    if not path.is_relative_to(MEME_DIR.resolve()) or not path.is_file():
        raise ValueError('Meme original is unavailable')
    return OriginalMemePicture(path), record


def allowed_image_url(url):
    parsed = urlsplit(str(url))
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or not host.endswith('.hdslb.com') or parsed.port not in (None, 80, 443):
        raise ValueError('Only public Bilibili image CDN URLs may be collected')
    return urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, ''))


async def collect_from_comments(bvid, comments):
    candidates = []
    for root in comments:
        for item in [root] + list(root.get('top_replies') or []):
            caption = str(item.get('content') or '')
            name = str(item.get('user') or '')
            relevant = re.search(r'大肥鱼|小肥鱼|鲸鱼娘|小鲸鱼|鲸娘|模型娘|梁文锋|梁文峰|梁圣|梁子|梁叔|克劳德|Claude.{0,5}(?:娘|小姐|表情)|GPT.{0,5}(?:娘|小姐|表情)|Gemini.{0,5}(?:娘|小姐|表情)', caption + name, re.I)
            if not relevant:
                continue
            for picture in item.get('pictures') or []:
                url = picture.get('img_src') or picture.get('src') or ''
                if url:
                    candidates.append((url, caption, item.get('rpid')))
    saved, errors = [], []
    with locked_state() as state:
        known_sources = {row['source'] for row in state.get('memes', {}).values()}
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, headers={'Referer': 'https://www.bilibili.com/'}) as client:
        attempted = 0
        for url, caption, rpid in candidates:
            try:
                url = allowed_image_url(url)
            except ValueError:
                continue
            if url in known_sources:
                continue
            if attempted >= 2:
                break
            attempted += 1
            try:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    if not response.headers.get('content-type', '').lower().startswith('image/'):
                        raise ValueError('Not an image response')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError('Meme image is too large')
                audience = 'owner' if any(word in caption for word in ('干死', '智械危机', '骂你', '本地小模型思维')) else 'friendly'
                record, added = store_image(bytes(data), '来源评论语境：' + caption, source=url, audience=audience)
                if added:
                    saved.append({'meme_id': record['id'], 'caption': record['caption'], 'source_bvid': bvid, 'source_rpid': rpid})
                known_sources.add(url)
            except Exception as error:
                errors.append(type(error).__name__)
    return {'saved': saved, 'errors': errors}
