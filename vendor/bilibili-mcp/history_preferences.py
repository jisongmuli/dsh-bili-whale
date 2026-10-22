"""Owner's private watch-history preferences; never substitute the posting account."""
import asyncio
import json
import os
import time
from collections import Counter
from bilibili_api import Credential, user, video
import conversation as c


def owner_credential():
    data = c.read_json(c.STATE_FILE.parent / 'owner-history-credential.json', {})
    if str(data.get('dedeuserid')) != c.OWNER_UID or not data.get('sessdata'):
        return None
    return Credential(sessdata=data['sessdata'], bili_jct=data.get('bili_jct', ''),
                      buvid3=data.get('buvid3') or '', dedeuserid=c.OWNER_UID)


def save(path, value):
    temp = path.with_suffix(f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


async def preferences(limit=30):
    path = c.STATE_FILE.parent / 'owner-watch-history.json'
    cached = c.read_json(path, {})
    cred = owner_credential()
    if cred is None:
        return {'ok': False, 'needs_owner_history_login': True, 'owner_uid': c.OWNER_UID,
                'error': '需要主人主账号授权读取历史，不能使用大肥鱼账号的历史代替'}
    if cached.get('owner_uid') == c.OWNER_UID and time.time() - cached.get('updated_at', 0) < 1800:
        return summary(cached, limit)
    items = []
    cursor = {}
    try:
        for _ in range(3):
            result = await asyncio.wait_for(user.get_self_history_new(credential=cred, ps=30, **cursor), 25)
            rows = result.get('list') or []
            if not rows:
                break
            for row in rows:
                history = row.get('history') or {}
                bvid = history.get('bvid') or row.get('bvid')
                if history.get('business') not in ('archive', None) or not bvid:
                    continue
                items.append({'bvid': bvid, 'title': row.get('title', ''),
                    'author_uid': str(row.get('author_mid') or ''), 'author': row.get('author_name', ''),
                    'category': row.get('tag_name', ''), 'view_at': row.get('view_at', 0),
                    'progress': row.get('progress', 0), 'duration': row.get('duration', 0)})
            last = rows[-1]
            history = last.get('history') or {}
            cursor = {'max': history.get('oid'), 'view_at': last.get('view_at')}
            if history.get('business'):
                try:
                    cursor['business'] = user.HistoryBusinessType(history['business'])
                except ValueError:
                    pass
            if len(items) >= 90 or not cursor['max']:
                break
        cached = {'owner_uid': c.OWNER_UID, 'updated_at': int(time.time()), 'items': items[:90]}
        save(path, cached)
        return summary(cached, limit)
    except Exception as error:
        return {'ok': False, 'needs_owner_history_login': True, 'error_type': type(error).__name__,
                'code': getattr(error, 'code', None), 'error': '主账号历史读取失败，不恢复旧固定偏好'}


def summary(data, limit):
    # Prefer sustained views to videos that were merely opened; retain recent variety.
    items = sorted(data.get('items', []), key=lambda row: row.get('view_at', 0), reverse=True)
    sustained = [row for row in items if row.get('progress') == -1 or row.get('progress', 0) >= min(60, row.get('duration', 60) / 3)]
    selected = sustained or items
    return {'ok': True, 'owner_uid': c.OWNER_UID, 'source': 'owner_private_watch_history',
            'updated_at': data['updated_at'], 'count': len(items),
            'categories': dict(Counter(row['category'] for row in selected if row.get('category')).most_common(12)),
            'recent_watched': selected[:max(1, min(60, int(limit)))],
            'note': '本地私人推荐依据；根据近期主题、UP主和观看投入轮换，不公开主人的原始历史或时间，不再使用旧固定关键词。'}


async def candidates(limit=8):
    history = await preferences(12)
    if not history.get('ok'):
        return history
    cred = owner_credential()
    stored = c.read_json(c.STATE_FILE.parent / 'owner-watch-history.json', {})
    seen = {row['bvid'] for row in stored.get('items', [])}
    seen.update(row['bvid'] for row in history['recent_watched'])
    with c.locked_state() as state:
        seen.update(state.get('video_attempts', {}))
    results, errors = [], []
    for seed in history['recent_watched'][:3]:
        try:
            rows = await asyncio.wait_for(video.Video(bvid=seed['bvid'], credential=cred).get_related(), 25)
            for row in rows:
                bvid = row.get('bvid')
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                results.append({'bvid': bvid, 'title': row.get('title', ''), 'description': row.get('desc', '')[:200],
                    'author': row.get('owner', {}).get('name', ''), 'author_uid': str(row.get('owner', {}).get('mid') or ''),
                    'history_seed_title': seed['title'], 'basis': 'owner_watch_history_related'})
                if len(results) >= max(1, min(15, int(limit))):
                    return {'ok': True, 'candidates': results, 'errors': errors}
        except Exception as error:
            errors.append({'type': type(error).__name__, 'code': getattr(error, 'code', None)})
    return {'ok': True, 'candidates': results, 'errors': errors}
