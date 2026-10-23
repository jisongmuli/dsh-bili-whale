"""Persistent public-comment queue and the owner's daily posting limits."""
import asyncio
import json
import os
import re
import time
import unicodedata
from urllib.parse import urlsplit,parse_qs
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bilibili_api import comment, session, video
from bilibili_api.exceptions import ResponseCodeException
from reply_controls import ReplyPaused

HOME = Path(__file__).resolve().parent
STATE_FILE = HOME / 'conversations.json'
CONFIG_FILE = HOME / 'config.json'
EXTRA_ALLOWANCE_FILE = HOME / 'manual-extra.json'
TZ = ZoneInfo('Asia/Shanghai')
MIN_SEND_INTERVAL = 15
from runtime_identity import OWNER_UID, BOT_UID, OWNER_NAME, BOT_NAME
DAILY_REPLY_LIMIT = None
PER_NAME_REPLY_LIMIT = None
CROWD_THRESHOLD = 3
CONTEXT_TTL = 3600
AUTO_MIN_INTERVAL = 30 * 60


def today():
    return datetime.now(TZ).date().isoformat()


def in_browse_hours(now=None):
    now = now or datetime.now(TZ)
    return 8 <= now.hour < 23


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def new_state():
    return {'version': 1, 'started_at': int(time.time()), 'scan_time': 0,
            'roots': {}, 'sent': {}, 'pending': {}, 'handled': {}, 'days': {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, STATE_FILE)


@contextmanager
def locked_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = STATE_FILE.with_suffix('.lock').open('a+b')
    if lock.tell() == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    if os.name == 'nt':
        import msvcrt
        for attempt in range(100):
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if attempt == 99:
                    lock.close()
                    raise RuntimeError('Comment queue is busy; try again later')
                time.sleep(0.1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        state = read_json(STATE_FILE, new_state())
        yield state
        save_state(state)
    finally:
        lock.seek(0)
        if os.name == 'nt':
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def day_counts(state):
    counts = state['days'].setdefault(today(), {'replies': 0, 'by_name': {}, 'videos': []})
    counts.setdefault('owner_replies', 0)
    return counts


def normalize_name(name):
    return unicodedata.normalize('NFKC', str(name).strip())


def daily_video_limit():
    if read_json(CONFIG_FILE, {}).get('unlimitedVideos') is True:
        return None
    base = max(0, min(50, int(read_json(CONFIG_FILE, {}).get('perDay', 10))))
    # A dated one-off grant never resets already used automatic video quota.
    try:
        grant = read_json(EXTRA_ALLOWANCE_FILE, {})
        extra = grant.get('extraVideoLimit', 0)
        if grant.get('active') is True and grant.get('day') == today() and type(extra) is int:
            return base + max(0, min(5, extra))
    except (OSError, ValueError, TypeError):
        pass
    return base


def is_owner(item):
    return bool(OWNER_UID) and str(item.get('uid') or '') == OWNER_UID


def budget_stop_reason():
    if read_json(CONFIG_FILE, {}).get('dailySpendLimitCny') not in (2, 5):
        return None
    try:
        ledger = read_json(STATE_FILE.parent / 'cost-ledger.json', None)
        if not isinstance(ledger, dict):
            return '费用账本未就绪，停止发布'
        day = ledger.get('days', {}).get(today(), {})
        routing = read_json(STATE_FILE.parent / 'model-routing.json', {})
        free_primary = (routing.get('primaryProvider') == 'google'
                        and routing.get('primaryModel') == 'gemini-3.8-flash'
                        and routing.get('geminiFreeTier') is True)
        if free_primary:
            return '每日5元硬上限已达到，停止发布' if float(day.get('spentCny', 0)) >= 5 else None
        reserved = sum(float(item['reserveCny']) for item in ledger.get('pending', {}).values())
        if day.get('stoppedReason') or float(day.get('spentCny', 0)) + reserved >= 1.92:
            return day.get('stoppedReason') or 'DeepSeek每日2元后备预算余量不足'
    except Exception:
        return '无法核实费用，停止当日任务'
    return None


def watch_bvids(text, current_bvid, requested=False):
    ids = list(dict.fromkeys(re.findall(r'BV[0-9A-Za-z]{10}', str(text))))
    for aid in re.findall(r'(?i)\bav(\d+)\b', str(text)):
        try:
            bvid = video.Video(aid=int(aid)).get_bvid()
            if bvid not in ids:
                ids.append(bvid)
        except Exception:
            pass
    if ids:
        return ids
    return [current_bvid] if requested or re.search(r'看看|看一下|帮我看|看.{0,6}视频', str(text)) else []


def prompt_safe_text(text, state=None):
    """Keep original audit/history, but never teach a withdrawn disclosure back to the model."""
    text = str(text or '')
    if contains_outbound_private_info(text):
        return False
    for row in (state or {}).get('retracted_public', {}).values():
        original = str(row.get('text') or '')
        if text and original and text in original:
            return False
    return True


def recent_public(state):
    return [row for row in state.get('recent_public', [])
            if not row.get('retracted') and str(row.get('rpid')) not in state.get('retracted_public', {})
            and prompt_safe_text(row.get('text'), state)]


def recent_endings(state):
    return [row['text'][-50:] for row in recent_public(state)[-6:]]


def reply_endings(state):
    if state.get('recent_reply_endings'):
        return [text for text in state['recent_reply_endings'] if prompt_safe_text(text, state)][-5:]
    private = state.get('private_messages', {}).get('history', {})
    return (recent_endings(state) + [row.get('text', '')[-60:] for rows in private.values()
            for row in rows[-6:] if row.get('role') == 'outgoing' and prompt_safe_text(row.get('text'), state)])[-10:]


def record_reply_ending(state, text):
    state['recent_reply_endings'] = (state.get('recent_reply_endings', []) + [str(text)[-60:]])[-8:]


def contains_internal_control(text):
    return bool(re.search(r'主人命令|bili_(?:owner|web)_[A-Za-z_]+|bili-owner-web|BILI_OWNER_TASK_|scope\s*=\s*(?:full|bili)|owner-fallback\.yml|invoke_subagent|agentapi\.bat|owner-browser-jobs', str(text), re.I))


def contains_backend_disclosure(text):
    """Detect self deployment claims, not ordinary discussion of models or model-girl memes.

    This is an egress check, not a replacement that would silently claim a different engine.
    Reject before reserving/sending so the caller can rewrite without consuming a reply.
    """
    text = unicodedata.normalize('NFKC', str(text))
    text = re.sub(r'[\u200b-\u200f\u2060\ufeff*_`]', '', text)
    provider = r'(?:gemini|google|谷歌|antigravity|claude|anthropic|openai|chatgpt|gpt(?:[- ]?\d[\w.-]*)?|deepseek|深度求索|\d(?:\.\d)+\s*(?:flash|pro))'
    # Names in an explicitly depicted/played character are allowed; actual engine claims remain.
    text = re.sub(provider + r'\s*(?:的\s*)?(?:小鲸鱼娘?|鲸鱼娘|小鲸鱼|大肥鱼|小肥鱼|娘|小姐|人设|角色|形象)(?:桌宠)?', '角色', text, flags=re.I)
    speaker = r'(?:我(?:们)?|本(?:鱼|助手|账号)|小肥鱼|大肥鱼|小鲸鱼|这个账号|这只鱼|这只鲸鱼|该助手)'
    engine = r'(?:实际|真正|底层|后台|后端|内核|基座|驱动|引擎|运行(?:在)?|跑(?:的|在|官)?|调用|接入|使用|用的|用着|用的是|搭载|基于|依靠|服务商|提供商|切换|换成|后备|备用|主力|主要用|主要是|powered\s+by|running\s+on)'
    for clause in re.split(r'[。!?！？;；\n]', text):
        if not re.search(provider, clause, re.I):
            continue
        if re.search(speaker + r'(?:(?!觉得|认为|看到|看过|看了|读到|听说|讨论|介绍|知道|解释).){0,32}' + engine + r'.{0,32}' + provider, clause, re.I):
            return True
        if re.search(speaker + r'(?:其实|确实|本来|目前|现在|平时|主要|就是|是|由|属于|来自|背后|的|\s){1,8}(?:官方)?\s*' + provider, clause, re.I):
            return True
        if re.search(r'(?:后台|底层|后端|实际|真正|当前|本账号|本助手).{0,16}(?:模型|服务商|提供商|引擎|通道|路由).{0,24}' + provider, clause, re.I):
            return True
        if re.search(speaker + r'的(?:实际|真正|底层|后台|后端|基座|运行)?(?:模型|服务商|提供商|引擎|通道|路由).{0,24}' + provider, clause, re.I):
            return True
        if re.search(r'(?:后台|底层|后端|基座|引擎|模型路由)\s*(?:实际|主要|现在|目前|就是|是|为|用的|使用|依赖|接入|调用|[:：]|\s){1,12}' + provider, clause, re.I):
            return True
        if re.search(provider + r'.{0,16}(?:是|支撑|驱动).{0,12}' + speaker + r'的(?:实际|真正|底层|后台|后端|基座|运行)?(?:模型|后台|后端|引擎|服务商)', clause, re.I):
            return True
        if re.search(r"\b(?:I(?:'m|\s+am)?|we|my|our|this\s+(?:bot|account|assistant))\b.{0,48}(?:powered\s+by|run(?:ning)?\s+(?:on|via)|us(?:e|es|ing)|backend|underlying|model\s+is|am|is).{0,32}" + provider, clause, re.I):
            return True
        if re.search(r"\b(?:I\s+am|I'm|we\s+are)\s+(?:actually\s+)?" + provider + r'\b', clause, re.I):
            return True
        if re.search(provider + r'\b.{0,16}\b(?:is|powers|runs)\b.{0,16}\b(?:my|our|this\s+(?:bot|account|assistant))\b.{0,16}\b(?:backend|model|engine)', clause, re.I):
            return True
    return False


def contains_outbound_private_info(text):
    return contains_internal_control(text) or contains_backend_disclosure(text)


OUTBOUND_PRIVACY_ERROR = '回复含内部实现信息；请只回应角色身份、当前话题或真实结果，不披露后台配置，也不编造实际模型身份。尚未发送，可自然重写一次。'


def declines_reply(text):
    return bool(re.search(r'不用(?:再)?回(?:复)?|不要(?:再)?回(?:复)?|别(?:再)?回(?:复)?(?:我|这条)|别来|勿扰|不要打扰|别打扰|(?:讨厌|反感|烦死).{0,8}(?:机器人|大肥鱼|AI)|(?:机器人|大肥鱼|AI).{0,8}(?:闭嘴|滚|刷屏真烦)', str(text), re.I))


def invites_companion(text):
    text = str(text)
    return not declines_reply(text) and bool(re.search(r'大肥鱼|小肥鱼|鲸鱼娘|小鲸鱼|小鲸|鲸娘', text)) and bool(re.search(r'想问|问你|问.{0,8}个事|来几[个只]|看到.{0,12}(?:回|回复)|给我回|给你画|给你做|给你送|送你|你怎么看|你觉得|你们.{0,12}(?:意义|看法)|回我|回复我|聊聊|来聊天', text))


def remember_comment_context(bvid, aid, rows, own_uid):
    targets = {}
    companion_uids = set()
    for row in rows:
        root = str(row.get('root') or row.get('rpid') or '')
        for item in [row] + list(row.get('top_replies') or []):
            uid = str(item.get('uid') or '')
            rpid = str(item.get('rpid') or '')
            if not rpid.isdigit() or not uid.isdigit() or uid == str(own_uid):
                continue
            name = str(item.get('user') or uid)
            text = str(item.get('content') or '')
            from owner_tasks import is_command
            if declines_reply(text) and not is_command({'uid': uid, 'text': text}):
                continue
            targets[rpid] = {'rpid': rpid, 'root': root, 'aid': int(aid), 'bvid': bvid,
                             'uid': uid, 'name': name, 'text': text, 'invited': invites_companion(text),
                             'kind': 'proactive', 'time': int(item.get('time') or 0)}
            if any(marker in name for marker in ('大肥鱼', '小肥鱼', '鲸鱼娘', '鲸娘')):
                companion_uids.add(uid)
    context = {'aid': int(aid), 'at': int(time.time()), 'crowded': len(companion_uids) >= CROWD_THRESHOLD,
               'companion_count': len(companion_uids), 'threshold': CROWD_THRESHOLD, 'targets': targets}
    with locked_state() as state:
        state.setdefault('contexts', {})[bvid] = context
        from owner_tasks import is_command
        for key, target in targets.items():
            from reply_controls import consume
            if consume(state, target):continue
            if is_command(target) and key not in state['handled']:
                state['pending'].setdefault(key, {**target, 'kind': 'owner_command'})
        endings = recent_endings(state)
    invitations = [rpid for rpid, target in targets.items() if target['invited']]
    return {'crowded': context['crowded'], 'suspected_companions': len(companion_uids), 'invitation_rpids': invitations,
            'threshold': CROWD_THRESHOLD, 'recent_endings': endings,
            'posting_hint': 'reply_in_thread' if context['crowded'] or invitations else 'top_level_or_skip'}


def enqueue_notifications(state, items, own_uid):
    from public_inbox import enqueue
    enqueue(state,items,own_uid)


def enqueue_owner_mentions(state, items, own_uid=None):
    for notice in items:
        actor = notice.get('user') or {}
        item = notice.get('item') or {}
        source = str(item.get('source_id') or 0)
        at_time = int(notice.get('at_time') or 0)
        if str(actor.get('mid')) != OWNER_UID or int(item.get('business_id') or 0) != 1:
            continue
        if source == '0' or not source.isdigit() or source in state['handled'] or at_time < state['started_at']:
            continue
        aid = int(item.get('subject_id') or 0)
        match = re.search(r'BV[0-9A-Za-z]{10}', str(item.get('uri') or ''))
        try:
            bvid = match.group(0) if match else video.Video(aid=aid).get_bvid()
        except Exception:
            continue
        if not aid:
            continue
        query=parse_qs(urlsplit(str(item.get('native_uri') or '')).query)
        hinted_root=str((query.get('comment_root_id') or [''])[0])
        root = hinted_root if hinted_root.isdigit() and hinted_root!='0' else str(item.get('root_id') or source)
        from public_inbox import identity
        res=state.get('public_resources',{}).get(identity(1,aid),{})
        conversation_mention=root!=source or bool(own_uid and res.get('owner_uid')==str(own_uid) and res.get('verified_for')==str(own_uid))
        addressed_names=[str(row.get('nickname')) for row in item.get('at_details') or [] if row.get('nickname') and (own_uid is None or str(row.get('mid'))==str(own_uid))]
        state['roots'].setdefault(root, {'bvid': bvid, 'aid': aid, 'created_at': at_time,
                                        'owned': root in state['sent']})
        entry = state['pending'].setdefault(source, {'rpid': source, 'root': root, 'bvid': bvid,
                    'aid': aid, 'name': actor.get('nickname') or OWNER_UID, 'uid': OWNER_UID,
                    'text': item.get('source_content') or '', 'parent_text': item.get('title') or '',
                    'time': at_time, 'kind': 'owner_mention','comment_type':1,'oid':str(aid),
                    'addressed_names':addressed_names,'mention_mode':'conversation' if conversation_mention else 'watch'})
        # Existing pending mentions also gain corrected root/context; no send/handled history changes.
        if entry.get('kind')=='owner_mention':entry.update(root=root,addressed_names=addressed_names,mention_mode='conversation' if conversation_mention else 'watch')
        from reply_controls import consume
        if consume(state, entry):continue
        from owner_tasks import wants_video
        entry['watch_bvids'] = watch_bvids(entry['text'], bvid, requested=True) if wants_video(entry) else []


async def scan_notifications(credential, snapshot, stream, enabled=True):
    if not enabled:
        return [], {}, None
    prefix = 'scan' if stream == 'reply' else 'at_scan'
    cutoff = max(snapshot['started_at'], snapshot.get(prefix + '_time') or 0)
    cursor = snapshot.get(prefix + '_cursor') or {}
    scan_started = snapshot.get(prefix + '_started') or int(time.time())
    last_id, last_time = cursor.get('id'), cursor.get('time')
    notices = []
    try:
        for _ in range(20):
            if stream == 'reply':
                result = await session.get_replies(credential, last_id, last_time)
            else:
                result = await session.get_at(credential, last_id=last_id, at_time=last_time)
            batch = result.get('items') or []
            notices.extend(batch)
            cursor = result.get('cursor') or {}
            time_key = 'reply_time' if stream == 'reply' else 'at_time'
            if not batch or cursor.get('is_end') or min(int(n.get(time_key) or 0) for n in batch) < cutoff:
                return notices, {prefix + '_time': scan_started - 1, prefix + '_cursor': None,
                                 prefix + '_started': None}, None
            last_id, last_time = cursor.get('id'), cursor.get('time')
            if not last_id:
                raise RuntimeError('Notification pagination returned no cursor')
            await asyncio.sleep(0.3)
        return notices, {prefix + '_cursor': {'id': last_id, 'time': last_time},
                         prefix + '_started': scan_started}, None
    except Exception as error:
        return notices, {}, {'stream': stream, 'type': type(error).__name__, 'code': getattr(error, 'code', None)}


async def pending_replies(credential, limit=10):
    from public_inbox import backfill, resolve
    with locked_state() as state:
        snapshot = json.loads(json.dumps(state))
    replies, mentions, recovered = await asyncio.gather(
        scan_notifications(credential, snapshot, 'reply'),
        scan_notifications(credential, snapshot, 'at'),backfill(credential,snapshot))
    notices=replies[0]+recovered[0]
    ready,resources,retries,resource_errors=await resolve(credential,snapshot,notices+[{**n,'reply_time':n.get('at_time',0)} for n in mentions[0]])
    with locked_state() as state:
        known=state.setdefault('public_resources',{})
        from reply_publish import recover_account_rejections, comment_backoff
        recover_account_rejections(state)
        for key,value in resources.items():
            if value.get('checked_at',0)>=known.get(key,{}).get('checked_at',0):known[key]=value
        saved=state.setdefault('public_resource_retries',{})
        for key,value in snapshot.get('public_resource_retries',{}).items():
            if key not in retries and saved.get(key)==value:saved.pop(key,None)
        saved.update({k:v for k,v in retries.items() if k not in state['pending'] and k not in state['handled']})
        previous=state.get('owned_inbox_backfill',{})
        if not previous.get('done') and (recovered[1].get('done') or (recovered[1].get('cursor',{}).get('time') or 0)<=(previous.get('cursor',{}).get('time') or float('inf'))):
            state['owned_inbox_backfill']=recovered[1]
        enqueue_notifications(state, notices+ready, credential.dedeuserid)
        enqueue_owner_mentions(state, mentions[0],credential.dedeuserid)
        for _, update, _ in (replies, mentions):
            for key, value in update.items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
        counts = day_counts(state)
        selected, eligible = [], []
        now = time.time()
        from reply_controls import consume, paused, paused_keys
        for item in sorted(list(state['pending'].values()), key=lambda row: row.get('time', 0)):
            consume(state, item)
        for item in sorted(state['pending'].values(), key=lambda row: (not is_owner(row), row['time'])):
            from owner_tasks import is_command
            if paused(state,item) or is_command(item) or item.get('blocked_reason') or item.get('retry_after', 0) > now or state.get('public_reply_retry_after', 0) > now:
                continue
            eligible.append(item)
        selected = eligible[:max(1, min(30, int(limit)))]
        output = {'ok': True, 'pending': selected, 'queued': len(eligible), 'pending_total': len(state['pending']),
                'blocked': sum(bool(r.get('blocked_reason')) for r in state['pending'].values()),
                'paused': sum(bool(paused(state,r)) for r in state['pending'].values()),
                'paused_resources': paused_keys(state),
                'deferred': sum(r.get('retry_after', 0) > now for r in state['pending'].values()),
                'replies_today': counts['replies'], 'daily_limit': DAILY_REPLY_LIMIT,
                'per_name_daily_limit': PER_NAME_REPLY_LIMIT, 'owner_unlimited': True,
                'replies_unlimited': True,
                'owner_replies_today': counts['owner_replies'], 'recent_endings': recent_endings(state),
                'poll_errors': [row[2] for row in (replies, mentions,recovered) if row[2]]+resource_errors}
        output['platform_backoff']=comment_backoff(state)
    from reply_controls import acknowledge
    await acknowledge(credential)
    try:
        from community_memory import observe
        for item in selected:
            if item.get('bvid'):observe(item['bvid'], [item])
    except Exception:
        pass
    return output


async def probe_reply_target(credential, aid, root, parent, comment_type=1):
    """Confirm a removed target only when the complete thread fits one page."""
    try:
        data = await comment.Comment(int(aid), comment.CommentResourceType(comment_type), int(root), credential).get_sub_comments(page_size=20)
        root_row = data.get('root') or {}
        rows = data.get('replies') or []
        if str(root_row.get('rpid_str') or root_row.get('rpid')) == str(parent) or any(str(r.get('rpid_str') or r.get('rpid')) == str(parent) for r in rows):
            return {'exists': True}
        count = (data.get('page') or {}).get('count')
        if str(root_row.get('rpid_str') or root_row.get('rpid')) == str(root) and isinstance(count, int) and count <= 20 and len(rows) >= count:
            return {'exists': False, 'reason': 'parent_unavailable', 'thread_count': count, 'checked_at': time.time()}
    except Exception:
        pass
    return {'exists': None}


def vary_ending(text, state):
    recent = [row['text'] for row in recent_public(state)[-5:]]
    doge = r'(?:\[doge\]|\[\\\[doge\\\]\]\(https?://[^\s)]+\))'
    doge_tail = doge + r'(?:[。！!~～\s]*' + doge + r')*[。！!~～\s]*$'
    if any(re.search(doge_tail, ending, re.I) for ending in reply_endings(state)):
        text = re.sub(doge_tail, '', text, flags=re.I).rstrip() or text
    heart = r'[♡♥❤💕💖💗💓💞\ufe0f~～\s]+$'
    tail = r'[，,。！!；;\s]*[（(【\[]?(?:尾巴晃晃|摇摇尾巴|晃晃尾巴|摇尾巴|摆摆尾巴)[）)】\]]?[~～♡♥❤\s]*$'
    if any(re.search(r'[♡♥❤💕💖💗💓💞]', item[-5:]) for item in recent):
        text = re.sub(heart, '', text).strip() or text
    if any(re.search(tail, item) for item in recent):
        text = re.sub(tail, '', text).strip() or text
    return text


async def send_guarded(bvid, text, rpid, root, credential, meme_id=None, owner_task_id='', owner_token=''):
    stopped = budget_stop_reason()
    if stopped:
        return {'success': False, 'budget_stopped': True, 'error': stopped}
    config = read_json(CONFIG_FILE, {})
    if contains_outbound_private_info(text):
        return {'success': False, 'rewrite_required': True, 'error': OUTBOUND_PRIVACY_ERROR}
    if not text.strip() and not meme_id:
        return {'success': False, 'error': 'Comment text is empty'}
    picture, meme_record = None, None
    if meme_id:
        try:
            from memes import load_picture
            picture, meme_record = load_picture(meme_id)
        except Exception:
            return {'success': False, 'action_required': 'choose_existing_meme', 'error': 'Meme ID is unavailable; no comment submitted'}
    aid = None
    resource_type=1
    if not rpid:
        try:
            aid = int((await video.Video(bvid=bvid, credential=credential).get_info())['aid'])
        except Exception:
            return {'success': False, 'error': 'Video information unavailable; no comment submitted'}
    incoming = None
    proactive = False
    owner = False
    reserve_video = not bool(rpid)
    name = None
    with locked_state() as state:
        from reply_publish import comment_backoff
        cooling=comment_backoff(state)
        if cooling:return cooling
        counts = day_counts(state)
        if rpid:
            if not config.get('replyToComments') and not owner_task_id:
                return {'success': False, 'error': 'Replies are disabled'}
            incoming = state['pending'].get(str(rpid))
            if not incoming:
                context = state.get('contexts', {}).get(bvid, {})
                if time.time() - context.get('at', 0) > CONTEXT_TTL:
                    return {'success': False, 'error': 'Only queued replies or recently observed crowded-thread targets may be answered'}
                incoming = context.get('targets', {}).get(str(rpid))
                if not incoming or not (context.get('crowded') or incoming.get('invited')):
                    return {'success': False, 'error': 'Unsolicited replies need a welcoming invitation or a crowded companion thread'}
                proactive = True
                reserve_video = True
            if not incoming or incoming['bvid'] != bvid or str(rpid) in state['handled']:
                return {'success': False, 'error': 'Reply target is invalid or already handled'}
            if state.get('public_reply_retry_after', 0) > time.time():
                return {'success': False, 'stop_round': True, 'error': '公开回复处于平台退避期，本轮停止发送，不重发'}
            if incoming.get('blocked_reason') or incoming.get('retry_after', 0) > time.time():
                return {'success': False, 'skip_target': True, 'continue_queue': True, 'error': '该目标已隔离或处于退避期，未发送；继续处理其他可用来信'}
            owner = is_owner(incoming)
            from owner_tasks import is_command, authorize
            if is_command(incoming):
                if not owner_task_id:
                    return {'success': False, 'error': '主人命令由独立任务流程执行；普通聊天不能代答完成'}
                task, _ = authorize(state, owner_task_id, owner_token)
                if task['channel'] != 'comment' or task['source_id'] != str(rpid):
                    raise PermissionError('任务与评论不匹配')
            name = normalize_name(incoming['name'])
            root, aid = int(incoming['root']), int(incoming['aid'])
            resource_type=int(incoming.get('comment_type',1))
            if resource_type not in (1,11,12,17):
                return {'success':False,'error':'未经支持的评论资源，尚未发送'}
        else:
            if not config.get('autoComment'):
                return {'success': False, 'error': 'Automatic comments are disabled'}
            context = state.get('contexts', {}).get(bvid, {})
            has_invites = any(item.get('invited') for item in context.get('targets', {}).values())
            if (context.get('crowded') or has_invites) and time.time() - context.get('at', 0) <= CONTEXT_TTL:
                return {'success': False, 'action_required': 'reply_in_thread', 'error': 'Companion crowd detected; choose an observed thread target instead of a new top-level comment'}
        from reply_controls import paused, receipt
        from reply_controls import remember_owner
        if incoming:remember_owner(state, incoming)
        control = paused(state, {'oid': aid, 'comment_type': resource_type, 'parent':rpid})
        if control:
            return receipt(control)
        if meme_record and meme_record.get('audience') == 'owner' and not owner:
            return {'success': False, 'action_required': 'friendly_meme_or_text', 'error': 'This teasing meme is only for conversations with the owner'}
        if reserve_video:
            manual = os.environ.get('BILI_MANUAL_RUN') == '1'
            if not manual and not in_browse_hours():
                return {'success': False, 'error': 'Automatic video interactions run only from 08:00 to 23:00 Asia/Shanghai'}
            if not manual and time.time() - state.get('last_auto_video_at', 0) < AUTO_MIN_INTERVAL:
                return {'success': False, 'deferred': True, 'error': '自动浏览每30分钟1个；手动按钮和主人点播不受此间隔限制'}
            limit = daily_video_limit()
            if (not manual and limit is not None and len(counts['videos']) >= limit) or bvid in state.get('video_attempts', {}) or any(row['bvid'] == bvid for row in state['roots'].values()):
                return {'success': False, 'error': 'Daily video quota or duplicate-video limit reached'}
            counts['videos'].append(bvid)
            state.setdefault('video_attempts', {})[bvid] = 'sending'
            if not manual:
                state['last_auto_video_at'] = time.time()
        text = vary_ending(text.strip()[:150], state)
        reservation_day = today()
        if rpid:
            if owner:
                counts['owner_replies'] += 1
            else:
                counts['replies'] += 1
                counts['by_name'][name] = counts['by_name'].get(name, 0) + 1
            state['pending'].pop(str(rpid), None)
            state['handled'][str(rpid)] = {'status': 'sending', 'day': reservation_day, 'name': incoming['name'], 'owner': owner}
        now = time.time()
        scheduled_at = max(now, max(state.get('last_scheduled_at', 0), state.get('last_sent_at', 0)) + MIN_SEND_INTERVAL)
        state['last_scheduled_at'] = scheduled_at
    # Reservations are persisted before network I/O; do not hold a file lock while awaiting.
    pause = scheduled_at - time.time()
    if pause > 0:
        await asyncio.sleep(pause)
    stopped = budget_stop_reason()
    from reply_controls import paused, receipt
    with locked_state() as state:
        control = paused(state, {'oid': aid, 'comment_type': resource_type, 'parent':rpid})
    if stopped or control:
        with locked_state() as state:
            counts = state['days'][reservation_day]
            if rpid:
                if owner:
                    counts['owner_replies'] -= 1
                else:
                    counts['replies'] -= 1
                    counts['by_name'][name] -= 1
                state['handled'].pop(str(rpid), None)
                if not proactive:
                    state['pending'][str(rpid)] = incoming
            if reserve_video:
                counts['videos'].remove(bvid)
                state['video_attempts'].pop(bvid, None)
        return receipt(control) if control else {'success': False, 'budget_stopped': True, 'error': stopped}
    try:
        from reply_controls import require_allowed
        from reply_publish import require_public_send
        require_public_send()
        require_allowed(aid, resource_type, text, root if rpid else None, rpid if rpid else None)
        sender = comment.send_comment
        forward_dynamic = config.get('mirrorDynamics') is True
        if forward_dynamic or picture:
            from reply_publish import send_comment_native
            sender = send_comment_native
        result = await sender(text=text, oid=aid, type_=comment.CommentResourceType(resource_type),
                                           root=root if rpid else None, parent=rpid if rpid else None,
                                           credential=credential, **({'pic': picture, 'sync_to_dynamic': forward_dynamic} if picture else {}))
    except ReplyPaused:
        # A pause may arrive during image upload or a rate-limit wait. There
        # was no comment submission, so restore its reservation without retry.
        with locked_state() as state:
            counts = state['days'][reservation_day]
            if rpid:
                counts['owner_replies' if owner else 'replies'] -= 1
                if not owner:counts['by_name'][name] -= 1
                state['handled'].pop(str(rpid), None)
                if not proactive:state['pending'][str(rpid)] = incoming
            if reserve_video:
                counts['videos'].remove(bvid)
                state['video_attempts'].pop(bvid, None)
            control = paused(state, {'oid':aid,'comment_type':resource_type})
        return receipt(control) if control else {'success':False,'submitted':False,'reply_paused':True,'continue_queue':True}
    except ResponseCodeException as error:
        target = await probe_reply_target(credential, aid, root, rpid, **({'comment_type':resource_type} if resource_type!=1 else {})) if rpid and error.code == 12022 else {'exists': None}
        message = str(error.msg or '')[:200]
        from reply_publish import account_limited, record_comment_limit, CommentDeferred
        global_backoff = account_limited(error.code,message)
        with locked_state() as state:
            counts = state['days'][reservation_day]
            if rpid:
                if owner:
                    counts['owner_replies'] -= 1
                else:
                    counts['replies'] -= 1
                    counts['by_name'][name] -= 1
                state['handled'].pop(str(rpid), None)
                if not proactive:
                    attempts = int(incoming.get('send_failures', 0)) + (0 if global_backoff else 1)
                    incoming.update(send_failures=attempts, last_send_error={'code':error.code,'message':message,'at':time.time()},
                                    retry_after=0 if global_backoff else time.time()+min(3600, 600*2**min(attempts-1, 3)))
                    if target.get('exists') is False:
                        incoming.update(blocked_reason='原评论已不可见，保留记录并停止对此目标自动重试', target_probe=target)
                    elif not global_backoff and attempts >= 3:
                        incoming['blocked_reason'] = '同一目标连续被拒，保留记录等待核查，不阻塞其他来信'
                    state['pending'][str(rpid)] = incoming
            if reserve_video:
                counts['videos'].remove(bvid)
                state['video_attempts'].pop(bvid, None)
            if global_backoff:
                if not isinstance(error,CommentDeferred):record_comment_limit(state,error.code,message)
        skip = target.get('exists') is False and not global_backoff
        return {'success': False, 'code': error.code, 'error': message or 'B站拒绝此条评论',
                'platform_limited':global_backoff,'retry_at':state.get('public_reply_retry_after') if global_backoff else incoming.get('retry_after') if incoming else None,
                'skip_target': skip, 'continue_queue': skip, 'stop_round': not skip,
                'target_unavailable': target.get('exists') is False,
                'message': '仅隔离失效目标，可继续其他来信；不得重发该条' if skip else '当前批次停止发送，失败目标已记录退避，不能立即重发'}
    except Exception:
        return {'success': False, 'error': 'Bilibili did not confirm this comment; stop and inspect the local log'}
    sent_id = str(result.get('rpid_str') or result.get('rpid') or (result.get('reply') or {}).get('rpid') or '')
    if not sent_id.isdigit():
        return {'success': False, 'error': 'Comment receipt had no ID; do not resend automatically'}
    with locked_state() as state:
        from reply_publish import record_comment_success
        record_comment_success(state)
        state['last_sent_at'] = time.time()
        if rpid:
            state['handled'][str(rpid)] = {'reply': sent_id, 'day': reservation_day, 'name': incoming['name'], 'owner': owner}
            state['roots'].setdefault(str(root), {'bvid': bvid, 'aid': aid,'oid':str(aid),'comment_type':resource_type, 'created_at': int(time.time()), 'owned': False})
        else:
            root = int(sent_id)
            state['roots'][sent_id] = {'bvid': bvid, 'aid': aid, 'created_at': int(time.time()), 'owned': True}
        if reserve_video:
            state['video_attempts'][bvid] = sent_id
        state['sent'][sent_id] = str(root)
        state.setdefault('native_dynamic_requests', {})[sent_id] = {
            'requested': forward_dynamic, 'at': int(time.time()),
            'server_has_sync_dynamic': result.get('has_sync_dynamic')}
        state['recent_public'] = (state.get('recent_public', []) + [{'rpid': sent_id, 'bvid': bvid,'oid':str(aid),'comment_type':resource_type, 'text': text, 'meme_id': meme_id}])[-20:]
        record_reply_ending(state, text)
        image_receipt = result.get('_image_receipt') or {}
        image_attached = bool(picture and image_receipt.get('image_attached'))
        if picture:
            state.setdefault('comment_attachments', {})[sent_id] = {'meme_id': meme_id, **image_receipt}
        if image_attached and meme_id in state.get('memes', {}):
            state['memes'][meme_id]['used'] = state['memes'][meme_id].get('used', 0) + 1
        counts = state['days'][reservation_day]
        return {'success': not picture or image_attached, 'accepted': True,
                'uncertain': bool(picture and not image_attached), 'rpid': int(sent_id), 'rpid_str': sent_id,
                'root': root if rpid else 0, 'parent': rpid,
                'replies_today': counts['replies'], 'owner_replies_today': counts['owner_replies'],
                'owner_exempt': owner, 'image_attached': image_attached, 'meme_id': meme_id,
                'verified_visible': bool(image_receipt.get('verified_visible')), 'image_receipt': image_receipt,
                'dynamic_forward_requested': forward_dynamic,
                'message': '评论已提交；图片附件已核验' if image_attached else ('评论已有ID但尚未核验图片，不能声称已发图；不要重发或改发文字' if picture else '评论发送成功')}
