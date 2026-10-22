"""Read recent uploads from the owner's selected meme-explainer accounts."""
import asyncio
import os
import time
from bilibili_api import user, search
import conversation as c


def config_file():
    return c.STATE_FILE.parent / 'community-sources.json'


def save_config(data):
    path = config_file()
    temp = path.with_suffix(f'.{os.getpid()}.tmp')
    temp.write_text(__import__('json').dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


async def feed(credential, limit=6):
    config = c.read_json(config_file(), {'creators': []})
    memory = c.read_json(c.STATE_FILE.parent / 'community-memory.json', {})
    observed = set(memory.get('videos', {}))
    if time.time() - config.get('feed_checked_at', 0) < 900:
        return {'ok': True, 'creators': config.get('creators', []),
                'videos': [row for row in config.get('feed_cache', []) if row['bvid'] not in observed][:limit], 'cached': True}
    videos, errors = [], []
    for source in config.get('creators', [])[:4]:
        try:
            creator = user.User(int(source['uid']), credential=credential)
            try:
                result = await asyncio.wait_for(creator.get_videos(ps=15), 20)
                items = (result.get('list') or {}).get('vlist') or []
            except Exception:
                # The regular upload-list endpoint sometimes challenges automated clients.
                result = await asyncio.wait_for(creator.get_media_list(ps=15), 20)
                items = result.get('media_list') or []
            for row in items:
                bvid = str(row.get('bvid') or '')
                title = str(row.get('title') or '')
                if not bvid or not title or not any(word in title for word in ('梗', '反串', '抽象', '盘点')):
                    continue
                videos.append({'bvid': bvid, 'title': title, 'author_uid': str(source['uid']),
                        'author': source['name'], 'published_at': row.get('created') or row.get('pubtime') or 0,
                        'title_is_clue_only': True})
        except Exception as exc:
            errors.append({'uid': str(source['uid']), 'type': type(exc).__name__, 'code': getattr(exc, 'code', None)})
    videos.sort(key=lambda row: row['published_at'], reverse=True)
    config.update(feed_cache=videos[:40], feed_checked_at=int(time.time()))
    save_config(config)
    return {'ok': True, 'creators': config.get('creators', []),
            'videos': [row for row in videos if row['bvid'] not in observed][:max(1, min(12, int(limit)))],
            'errors': errors, 'note': '标题只用于找梗词，含义要结合视频解释和当前/过往评论，不凭标题编造出处。'}
