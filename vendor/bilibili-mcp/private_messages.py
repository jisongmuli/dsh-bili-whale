"""Reply to incoming one-to-one messages using the public reply quota and lock.

Private text stays in this local section. It is never observed by community_memory,
added to recent_public, or passed to the native comment/dynamic publisher.
"""
import asyncio
import copy
import json
import time
import uuid

from bilibili_api import session, user
from bilibili_api.exceptions import ResponseCodeException
from bilibili_api.utils.network import Api

import conversation as c


def number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def enabled():
    return c.read_json(c.CONFIG_FILE, {}).get('replyToPrivateMessages') is True


def section(state):
    return state.setdefault('private_messages', {
        'started_at': int(time.time()), 'sessions': {}, 'pending': {},
        'handled': {}, 'history': {}, 'names': {},
    })


def content_of(message):
    raw = message.get('content') or {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}


def message_id(uid, message):
    # Keep opaque IDs as strings all the way through MCP/JavaScript (>2**53).
    key = str(message.get('msg_key') or '')
    seq = number(message.get('msg_seqno'))
    return f'dm:{uid}:{seq}:{key}' if key.isdigit() and seq > 0 else ''


def ingest(dm, uid, name, messages, own_uid):
    """Only a server-addressed incoming user message can become a reply target."""
    for message in sorted(messages, key=lambda row: number(row.get('msg_seqno'))):
        if (str(message.get('sender_uid')) != uid or uid == own_uid
                or number(message.get('receiver_type')) != 1
                or str(message.get('receiver_id')) != own_uid):
            continue
        kind = number(message.get('msg_type'))
        body = content_of(message)
        if not isinstance(body, dict):
            continue
        if kind == 5:
            withdrawn = str(body.get('content') or '')
            known=set(dm['pending'])|set(dm['handled'])|set(dm.get('images',{}))
            known.update(row.get('message_id','') for row in dm['history'].get(uid,[]))
            known.update(row.get('message_id','') for row in dm.get('recent_images',{}).get(uid,[]))
            for key in known:
                if key.startswith('dm:'+uid+':') and key.rsplit(':',1)[-1] == withdrawn:
                    dm['pending'].pop(key,None)
                    dm['handled'][key] = {**dm['handled'].get(key,{}),'status': 'withdrawn', 'day': c.today()}
            if uid in dm.get('recent_images',{}):
                dm['recent_images'][uid]=[r for r in dm['recent_images'][uid] if r['message_id'].rsplit(':',1)[-1]!=withdrawn]
            dm['history'][uid] = [row for row in dm['history'].get(uid, [])
                                  if str(row.get('msg_key')) != withdrawn]
            continue
        key = message_id(uid, message)
        if not key or key in dm['handled'] or key in dm['pending']:
            continue
        if number(message.get('msg_status')) == 1:
            dm['handled'][key] = {'status': 'withdrawn', 'day': c.today()}
            continue
        if kind == 1:
            text = str(body.get('content') or '')[:6000]
        elif kind == 2:
            text = '[对方发来图片；请调用图片识别工具读取实际图片后再回应]'
        elif kind == 7:
            text = '[分享视频] ' + str(body.get('title') or '')[:300]
            bvid = str(body.get('bvid') or '')
            if bvid.startswith('BV') and len(bvid) == 12:
                text += ' ' + bvid
        else:
            # System notices, promotional pushes, groups and welcome cards are not conversations.
            continue
        from owner_tasks import is_command
        if not text.strip() or (c.declines_reply(text) and not is_command({'uid': uid, 'text': text})):
            dm['handled'][key] = {'status': 'declined', 'day': c.today()}
            continue
        item = {'message_id': key, 'msg_key': str(message['msg_key']),
                'seqno': number(message['msg_seqno']), 'uid': uid, 'name': name,
                'text': text, 'message_type': kind,
                'time': number(message.get('timestamp')), 'channel': 'private'}
        if kind == 2:
            try:
                from private_images import image_url
                item['image']={'url':image_url(body.get('url')),'width':body.get('width'),'height':body.get('height')}
            except ValueError:
                item['image_unavailable']=True
        if uid == c.OWNER_UID:
            # Bind context when the real message arrives. Later traffic may roll
            # the short chat history, but cannot change what this command meant.
            item['context_before']=copy.deepcopy(dm['history'].get(uid,[])[-12:])
            recent=dm.setdefault('recent_images',{}).setdefault(uid,[])
            eligible=[r for r in recent if item['time']-1800<=r.get('time',0)<=item['time']
                and number(r['message_id'].split(':')[2])<item['seqno']
                and dm['handled'].get(r['message_id'],{}).get('status')!='withdrawn'][-8:]
            item['image_refs']=copy.deepcopy(eligible)
            if kind==2 and item.get('image'):
                recent.append({'message_id':key,'time':item['time'],'image':copy.deepcopy(item['image'])})
                recent[:]=sorted(recent,key=lambda r:number(r['message_id'].split(':')[2]))[-8:]
        dm['pending'][key] = item
        if uid == c.OWNER_UID:
            dm['last_owner_activity'] = max(dm.get('last_owner_activity', 0), item['time'])
        dm['history'][uid] = (dm['history'].get(uid, []) + [
            {'role': 'incoming', 'message_id': key, 'msg_key': item['msg_key'], 'text': text, 'time':item['time']}])[-12:]


# Credentials are passed explicitly to API helpers; never persisted in the queue.
async def session_page(credential, end_ts=None):
    params = dict(session_type=4, group_fold=1, unfollow_fold=0, sort_rule=2,
                  size=100, build=0, mobi_app='web')
    if end_ts:
        params['end_ts'] = end_ts
    return await Api(**session.API['session']['get'], credential=credential).update_params(**params).result


async def message_page(credential, uid, begin, end=None):
    params = dict(talker_id=int(uid), session_type=1, size=200,
                  begin_seqno=begin, build=0, mobi_app='web')
    if end:
        params['end_seqno'] = end
    return await Api(**session.API['session']['fetch'], credential=credential).update_params(**params).result


async def resolve_name(credential, uid, cached):
    if uid == c.OWNER_UID:
        return c.OWNER_NAME
    if cached and time.time() - cached.get('at', 0) < 86400:
        return cached.get('name')
    info = await user.User(int(uid), credential=credential).get_user_info()
    return str(info.get('name') or '').strip() or None


async def poll(credential):
    lease = str(uuid.uuid4())
    with c.locked_state() as state:
        dm = section(state)
        if dm.get('scanning_until', 0) > time.time():
            return []
        dm.update(scanning=lease, scanning_until=time.time() + 600)
        list_cursor = dm.get('list_cursor')
    errors = []
    try:
        front = await session_page(credential)
        pages = [front]
        if list_cursor:
            pages.append(await session_page(credential, list_cursor))
        tail = pages[-1]
        tail_rows = tail.get('session_list') or []
        cursor = min((number(row.get('session_ts')) for row in tail_rows), default=0)
        with c.locked_state() as state:
            section(state)['list_cursor'] = cursor if tail.get('has_more') and cursor and cursor != list_cursor else None
        rows = {str(row.get('talker_id')): row for page in pages
                for row in (page.get('session_list') or [])}
        ordered = sorted(rows.items(), key=lambda pair: (pair[0] != c.OWNER_UID,
                                                         -number(pair[1].get('session_ts'))))
        for uid, row in ordered:
            if (not uid.isdigit() or uid == str(credential.dedeuserid)
                    or number(row.get('session_type')) != 1
                    or number(row.get('system_msg_type')) != 0):
                continue
            maximum = number(row.get('max_seqno'))
            with c.locked_state() as state:
                dm = section(state)
                saved = json.loads(json.dumps(dm['sessions'].get(uid, {})))
                cached = dm['names'].get(uid)
                baseline = saved.get('cursor')
                if baseline is None:
                    last = row.get('last_msg') or {}
                    fresh = number(last.get('timestamp')) >= dm['started_at']
                    baseline = number(row.get('ack_seqno')) if number(row.get('unread_count')) or fresh else maximum
                    dm['sessions'][uid] = {'cursor': baseline}
            backlog = saved.get('backlog')
            if maximum <= baseline and not backlog:
                continue
            try:
                try:
                    name = await resolve_name(credential, uid, cached)
                except Exception:
                    name = None
                name = name or (cached or {}).get('name') or uid
                target = backlog['target'] if backlog else maximum
                end = backlog.get('end') if backlog else None
                messages = backlog.get('messages', []) if backlog else []
                complete = False
                for _ in range(5):
                    page = await message_page(credential, uid, baseline, end)
                    batch = page.get('messages') or []
                    relevant = [msg for msg in batch if baseline < number(msg.get('msg_seqno')) <= target]
                    messages.extend(relevant)
                    low = min((number(msg.get('msg_seqno')) for msg in batch), default=0)
                    if not batch or not page.get('has_more') or low <= baseline + 1:
                        complete = True
                        break
                    if not low or (end and low >= end):
                        raise ValueError('Private message pagination did not advance')
                    end = low
                with c.locked_state() as state:
                    dm = section(state)
                    dm['names'][uid] = {'name': name, 'at': int(time.time())}
                    current = dm['sessions'].setdefault(uid, {'cursor': baseline})
                    if complete:
                        ingest(dm, uid, name, messages, str(credential.dedeuserid))
                        current.update(cursor=max(current['cursor'], target))
                        current.pop('backlog', None)
                    else:
                        current['backlog'] = {'target': target, 'end': end, 'messages': messages}
            except Exception as error:
                errors.append({'stream': 'private', 'uid': uid, 'type': type(error).__name__,
                               'code': getattr(error, 'code', None)})
    except Exception as error:
        errors.append({'stream': 'private', 'type': type(error).__name__, 'code': getattr(error, 'code', None)})
    finally:
        with c.locked_state() as state:
            dm = section(state)
            if dm.get('scanning') == lease:
                dm.pop('scanning', None)
                dm.pop('scanning_until', None)
            dm['checked_at'] = int(time.time())
            dm['poll_errors'] = errors
    return errors


def select(state, limit=10):
    dm = section(state)
    counts = c.day_counts(state)
    chosen = []
    for item in sorted(dm['pending'].values(), key=lambda row: (not c.is_owner(row), row['time'], row['seqno'])):
        from owner_tasks import is_command
        if is_command(item):
            continue
        if item.get('blocked_reason'):
            continue
        context = [{**row, 'text': str(row.get('text') or '')[:1000]}
                   for row in dm['history'].get(item['uid'], [])[-6:]
                   if row.get('role') != 'outgoing' or c.prompt_safe_text(row.get('text'), state)]
        chosen.append({**item, 'text': item['text'][:3000],
                       'text_truncated': len(item['text']) > 3000, 'context': context})
        if c.is_owner(item):
            chosen[-1]['owner_tasks_context']=[{'task_id':t['id'],'command':t.get('command'),'status':t['status'],
                'confirmed_actions':[a.get('params') for a in t.get('actions',{}).values() if a.get('status')=='success']}
                for t in sorted(state.get('owner_tasks',{}).values(),key=lambda t:t.get('claimed_at',0),reverse=True)[:3]]
        if len(chosen) >= max(1, min(number(limit, 10), 10)):
            break
    return {'pending': chosen, 'queued': len(dm['pending']),
            'blocked': sum(bool(item.get('blocked_reason')) for item in dm['pending'].values()),
            'replies_today': counts['replies'], 'owner_replies_today': counts['owner_replies'],
            'dm_replies_today': counts.get('dm_replies', 0),
            'owner_dm_replies_today': counts.get('owner_dm_replies', 0),
            'recent_endings': c.reply_endings(state),
            'shared_daily_limit': c.DAILY_REPLY_LIMIT, 'per_name_daily_limit': c.PER_NAME_REPLY_LIMIT}


async def pending_private_messages(credential, limit=10):
    if not enabled():
        return {'pending': [], 'queued': 0, 'disabled': True}
    errors = await poll(credential)
    with c.locked_state() as state:
        return {**select(state, limit), 'poll_errors': errors}


def rollback(state, incoming, day):
    dm = section(state)
    counts = state['days'][day]
    if c.is_owner(incoming):
        counts['owner_replies'] -= 1
        counts['owner_dm_replies'] -= 1
    else:
        counts['replies'] -= 1
        counts['by_name'][c.normalize_name(incoming['name'])] -= 1
        counts['dm_replies'] -= 1
    dm['handled'].pop(incoming['message_id'], None)
    dm['pending'][incoming['message_id']] = incoming


async def send_guarded(message_id, text, credential, owner_task_id='', owner_token='', meme_id=''):
    if not enabled() and not owner_task_id:
        return {'success': False, 'disabled': True}
    reason = c.budget_stop_reason()
    if reason:
        return {'success': False, 'budget_stopped': True, 'error': reason}
    text = str(text).strip()
    if c.contains_outbound_private_info(text):
        return {'success': False, 'rewrite_required': True, 'error': c.OUTBOUND_PRIVACY_ERROR}
    if (not text and not meme_id) or len(text) > 150 or (text and meme_id):
        return {'success': False, 'error': '私信回复须为1–150字'}
    picture,meme=None,None
    if meme_id:
        try:
            from memes import load_picture
            picture,meme=load_picture(meme_id)
        except Exception:
            return {'success':False,'error':'所选表情图不可用，请选择已有图片或改为文字'}
    with c.locked_state() as state:
        dm = section(state)
        incoming = dm['pending'].get(str(message_id))
        if not incoming or str(message_id) in dm['handled'] or incoming.get('blocked_reason'):
            return {'success': False, 'error': '不是可回复的实际来信，或已处理；不要重发'}
        from owner_tasks import is_command, authorize
        if is_command(incoming):
            if not owner_task_id:
                return {'success': False, 'error': '主人命令由独立任务流程执行；普通聊天不能代答完成'}
            task, _ = authorize(state, owner_task_id, owner_token)
            if task['channel'] != 'dm' or task['source_id'] != str(message_id):
                raise PermissionError('任务与来信不匹配')
        text = c.vary_ending(text, state)
        uid = str(incoming['uid'])
        if not uid.isdigit() or uid == str(credential.dedeuserid):
            return {'success': False, 'error': '私信收件人无效'}
        owner = c.is_owner(incoming)
        if meme and meme.get('audience')=='owner' and not owner:
            return {'success':False,'error':'这张玩笑图只适合主人，不能发给其他人'}
        if meme:
            text='[发送表情包：'+meme.get('caption','')[:100]+']'
        name = c.normalize_name(incoming['name'])
        counts = c.day_counts(state)
        day = c.today()
        if owner:
            counts['owner_replies'] += 1
            counts['owner_dm_replies'] = counts.get('owner_dm_replies', 0) + 1
        else:
            counts['replies'] += 1
            counts['by_name'][name] = counts['by_name'].get(name, 0) + 1
            counts['dm_replies'] = counts.get('dm_replies', 0) + 1
        dm['pending'].pop(str(message_id))
        dm['handled'][str(message_id)] = {'status': 'sending', 'day': day, 'uid': uid}
        scheduled = max(time.time(), state.get('last_scheduled_at', 0) + c.MIN_SEND_INTERVAL,
                        state.get('last_sent_at', 0) + c.MIN_SEND_INTERVAL)
        state['last_scheduled_at'] = scheduled
    await asyncio.sleep(max(0, scheduled - time.time()))
    reason = c.budget_stop_reason()
    if reason or (not enabled() and not owner_task_id):
        with c.locked_state() as state:
            rollback(state, incoming, day)
        return {'success': False, 'budget_stopped': bool(reason), 'error': reason or '私信回复已关闭'}
    try:
        result = await session.send_msg(credential=credential, receiver_id=int(uid),
            msg_type=session.EventType.PICTURE if picture else session.EventType.TEXT, content=picture or text)
    except ResponseCodeException as error:
        with c.locked_state() as state:
            rollback(state, incoming, day)
            section(state)['pending'][str(message_id)]['blocked_reason'] = f'Bilibili rejected private reply: {error.code}'
        return {'success': False, 'error': f'B站拒绝私信（{error.code}）；保留来信并停止自动重试'}
    except Exception:
        # The server may have accepted it: retain quota and dedup reservation.
        with c.locked_state() as state:
            section(state)['handled'][str(message_id)]['status'] = 'uncertain'
        return {'success': False, 'error': '私信回执不明，已保留额度且不会自动重发'}
    receipt = str((result or {}).get('msg_key') or '')
    with c.locked_state() as state:
        dm = section(state)
        record = dm['handled'][str(message_id)]
        if not receipt.isdigit():
            record['status'] = 'uncertain'
            return {'success': False, 'error': '私信回执缺少消息ID，不自动重发'}
        record.update(status='sent', reply_msg_key=receipt)
        if owner:
            dm['last_owner_activity'] = time.time()
        if meme_id and meme_id in state.get('memes',{}):
            state['memes'][meme_id]['used']=state['memes'][meme_id].get('used',0)+1
        c.record_reply_ending(state, text)
        state['last_sent_at'] = time.time()
        dm['history'][uid] = (dm['history'].get(uid, []) + [
            {'role': 'outgoing', 'in_reply_to': str(message_id), 'msg_key': receipt, 'text': text}])[-12:]
        return {'success': True, 'message_id': str(message_id), 'reply_msg_key': receipt,
                'image_sent':bool(picture),'meme_id':meme_id or None,
                'owner_exempt': owner, 'replies_today': state['days'][day]['replies'],
                'owner_replies_today': state['days'][day]['owner_replies'],
                'shared_daily_limit': c.DAILY_REPLY_LIMIT, 'message': '私信回复成功；不会转发动态'}
