"""Owner-only, persistent random conversation invitations (never a task grant)."""
import asyncio
import hashlib
import json
import os
import random
import time
import secrets
from datetime import datetime

from bilibili_api import session
from bilibili_api.exceptions import ResponseCodeException
import conversation as c


def settings():
    saved=c.read_json(c.HOME/'owner-checkins-settings.json', {})
    config=c.read_json(c.HOME/'config.json',{})
    return {**saved,'enabled':config.get('ownerCheckinsEnabled',saved.get('enabled',False))}


def plan(state, now):
    date = datetime.fromtimestamp(now, c.TZ)
    day = date.date().isoformat()
    data = state.setdefault('owner_checkins', {'days': {}, 'recent': []})
    if day not in data['days']:
        start = date.replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
        end = date.replace(hour=23, minute=0, second=0, microsecond=0).timestamp()
        count = random.SystemRandom().randint(10, 16)
        moments = sorted(random.SystemRandom().uniform(start, end) for _ in range(count))
        # One randomized first invitation after the feature is enabled, not on every reboot.
        if settings().get('welcome_on') == day and start <= now < end-120:
            future = next((i for i,t in enumerate(moments) if t >= now), None)
            if future is not None:
                moments[future] = now+random.SystemRandom().uniform(30, 90)
                moments.sort()
        data['days'][day] = {'target': count, 'slots': [
            {'id': hashlib.sha256(f'{day}:{i}:{t}'.encode()).hexdigest()[:24],
             'due_at': t, 'status': 'skipped_offline' if t < now-600 else 'pending'}
            for i, t in enumerate(moments)]}
    return data, data['days'][day]


def eligible(state, now=None):
    now = time.time() if now is None else now
    if settings().get('enabled') is not True:
        return {'due': False, 'reason': 'disabled'}
    data, day = plan(state, now)
    slots = day['slots']
    # Expired/offline invitations are discarded, never replayed as a burst.
    for slot in slots:
        if slot['status'] == 'pending' and slot['due_at'] < now-600:
            slot['status'] = 'skipped_offline'
    upcoming = [s['due_at'] for s in slots if s['status'] == 'pending' and s['due_at'] > now]
    result = {'due': False, 'target_today': day['target'],
              'sent_today': sum(s['status'] == 'sent' for s in slots),
              'next_at': min(upcoming, default=None), 'hours': '08:00–23:00', 'random': True}
    if not c.in_browse_hours(datetime.fromtimestamp(now, c.TZ)):
        return {**result, 'reason': 'quiet_hours'}
    dm = state.get('private_messages', {})
    if any(str(row.get('uid')) == c.OWNER_UID and not row.get('blocked_reason') for row in dm.get('pending', {}).values()):
        return {**result, 'reason': 'owner_message_pending'}
    if any(str(row.get('uid')) == c.OWNER_UID and not row.get('blocked_reason') for row in state.get('pending', {}).values()):
        return {**result, 'reason': 'owner_message_pending'}
    if any(job.get('status') in ('running', 'queued') for job in state.get('owner_tasks', {}).values()):
        return {**result, 'reason': 'owner_task_running'}
    if now < max(dm.get('last_owner_activity', 0), data.get('last_sent_at', 0)) + 180:
        return {**result, 'reason': 'conversation_active'}
    if now < data.get('retry_after', 0):
        return {**result, 'reason': 'platform_backoff'}
    ready = [s for s in slots if s['status'] == 'pending' and s['due_at'] <= now]
    if not ready:
        return {**result, 'reason': 'waiting_random_time'}
    # Even if polling was delayed, only the latest slot remains actionable.
    for slot in ready[:-1]:
        slot['status'] = 'skipped_late'
    return {**result, 'due': True, 'slot_id': ready[-1]['id']}


def context():
    with c.locked_state() as state:
        due = eligible(state)
        if not due['due']:
            return due
        history = [row for row in state.get('private_messages', {}).get('history', {}).get(c.OWNER_UID, [])[-8:]
                   if row.get('role') != 'outgoing' or c.prompt_safe_text(row.get('text'), state)]
        recent = state.get('owner_checkins', {}).get('recent', [])[-10:]
        result = {**due, 'owner_uid': c.OWNER_UID, 'local_time':datetime.now(c.TZ).isoformat(), 'private_context': history,
                  'recent_invitations': recent,
                  'recent_endings': [str(row.get('text',''))[-60:] for row in history if row.get('role')=='outgoing'][-5:]}
    # Only successfully analysed real media may support "I watched/saw" claims.
    videos = []
    for file in sorted((c.HOME/'video-cache').glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:60]:
        try:
            row = c.read_json(file, {})
            if row.get('ok') and row.get('analysed_at', 0) > time.time()-7*86400:
                analysis = row.get('analysis') or {}
                from character_memory import matches
                videos.append({'bvid': row['bvid'], 'title': row.get('title'), 'summary': analysis.get('summary'),
                               'recognized_characters':matches(analysis,'video:'+row['bvid']),
                               'uncertainties': analysis.get('uncertainties', []), 'analysed_at': row.get('analysed_at')})
        except (ValueError, OSError, KeyError):
            continue
        if len(videos) == 4:
            break
    from character_memory import catalog
    return {**result, 'actually_watched': videos,'character_memory':catalog(), 'privacy': 'owner_private_only'}


async def dispatch(credential, mode='status', slot_id='', text='', token=''):
    if mode == 'status':
        with c.locked_state() as state:
            return eligible(state)
    if mode in ('context', 'claim'):
        if os.environ.get('BILI_MCP_TOOLS'):
            return {'success': False, 'error': '主动搭话只由独立私聊轮启动'}
        result = context()
        if mode == 'claim' and result.get('due'):
            with c.locked_state() as state:
                data, day = plan(state,time.time())
                if data.get('lease_until',0) > time.time():
                    return {'due':False,'reason':'claimed'}
                slot=next(s for s in day['slots'] if s['id']==result['slot_id'])
                slot['token']=secrets.token_hex(16)
                data['lease_until']=time.time()+180
                result['token']=slot['token']
        return result
    if mode not in ('send', 'skip'):
        return {'success': False, 'error': '无效操作'}
    text = str(text).strip()
    if mode == 'send' and (not text or len(text) > 150 or c.contains_outbound_private_info(text)):
        return {'success': False, 'rewrite_required': True, 'error': '请用150字以内自然私聊，不透露内部信息'}
    with c.locked_state() as state:
        due = eligible(state)
        if not due.get('due') or due.get('slot_id') != str(slot_id):
            return {'success': False, 'not_due': True, 'reason': due.get('reason')}
        data, day = plan(state, time.time())
        slot = next(s for s in day['slots'] if s['id'] == slot_id)
        if not token or not secrets.compare_digest(str(slot.get('token','')),str(token)):
            return {'success':False,'error':'主动私聊机会已失效'}
        if mode == 'skip':
            slot['status'] = 'skipped_no_topic'
            data['lease_until']=0
            return {'success': True, 'skipped': True}
        text = c.vary_ending(text, state)
        if any(text == row.get('text') for row in data['recent'][-10:]):
            return {'success': False, 'rewrite_required': True, 'error': '近期已发过相同话题，请自然换一个'}
        if c.budget_stop_reason():
            return {'success': False, 'budget_stopped': True}
        slot.update(status='sending', text=text, started_at=time.time())
        data['lease_until']=0
        scheduled = max(time.time(), state.get('last_scheduled_at', 0)+c.MIN_SEND_INTERVAL,
                        state.get('last_sent_at', 0)+c.MIN_SEND_INTERVAL)
        state['last_scheduled_at'] = scheduled
    await asyncio.sleep(max(0, scheduled-time.time()))
    # Incoming messages always win, including those received while waiting to send.
    with c.locked_state() as state:
        data, day = plan(state, time.time())
        slot = next(s for s in day['slots'] if s['id'] == slot_id)
        owner_pending = any(str(r.get('uid')) == c.OWNER_UID for r in state.get('private_messages', {}).get('pending', {}).values())
        if owner_pending or not c.in_browse_hours() or settings().get('enabled') is not True or c.budget_stop_reason():
            slot['status'] = 'skipped_conversation'
            return {'success': False, 'cancelled_before_send': True}
    try:
        receipt = await session.send_msg(credential=credential, receiver_id=int(c.OWNER_UID),
            msg_type=session.EventType.TEXT, content=text)
    except ResponseCodeException as error:
        with c.locked_state() as state:
            data, day = plan(state, time.time())
            next(s for s in day['slots'] if s['id'] == slot_id).update(status='rejected', code=error.code)
            data['retry_after'] = time.time()+3600
        return {'success': False, 'rejected': True, 'code': error.code}
    except Exception:
        receipt = {}
    key = str((receipt or {}).get('msg_key') or '')
    with c.locked_state() as state:
        data, day = plan(state, time.time())
        slot = next(s for s in day['slots'] if s['id'] == slot_id)
        slot.update(status='sent' if key.isdigit() else 'uncertain', reply_msg_key=key, finished_at=time.time())
        if not key.isdigit():
            data['retry_after'] = time.time()+3600
            return {'success': False, 'uncertain': True, 'message': '回执不明，不重发'}
        data['last_sent_at'] = state['last_sent_at'] = time.time()
        data['recent'] = (data['recent']+[{'text':text,'msg_key':key,'at':time.time()}])[-20:]
        dm = state.setdefault('private_messages', {})
        histories = dm.setdefault('history', {})
        histories[c.OWNER_UID] = (histories.get(c.OWNER_UID, [])+[{
            'role':'outgoing','proactive':True,'msg_key':key,'text':text,'time':time.time()}])[-12:]
        c.record_reply_ending(state, text)
        return {'success':True,'reply_msg_key':key,'owner_uid':c.OWNER_UID,'proactive':True}
