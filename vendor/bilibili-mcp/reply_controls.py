"""Durable, owner-authorized public reply controls, keyed by platform resource.

Incoming control messages are consumed before model scheduling. Every publishing
route checks the same record again immediately before submitting a comment.
"""
import re
import time
import unicodedata
import asyncio
import uuid
import os
from contextvars import ContextVar

_ack = ContextVar('reply_control_ack', default=None)
ACK_TEXT = {'pause': '收到主人命令，该回家吃饭啦，这个评论区先暂停回复。',
            'resume': '收到主人命令，这个评论区恢复回复啦。'}


class ReplyPaused(PermissionError):
    pass


def resource_key(item):
    try:
        type_ = int(item.get('comment_type', item.get('type', 1)))
    except (TypeError, ValueError):
        return ''
    oid = str(item.get('oid') or item.get('aid') or '')
    if type_ in (1, 11, 12, 17) and oid.isdigit() and int(oid) > 0:
        return f'{type_}:{oid}'
    return ''


def remember_owner(state, item):
    import conversation as c
    if c.is_owner(item) and item.get('rpid') and resource_key(item):
        state.setdefault('owner_reply_sources', {})[str(item['rpid'])] = {
            'uid': str(item['uid']), 'resource_key': resource_key(item),
            'root': str(item.get('root') or item['rpid'])}


def owner_target(state, item, parent):
    row = state.get('owner_reply_sources', {}).get(str(parent), {})
    return bool(row and row['resource_key'] == resource_key(item))


def event_order(value):
    source = str(value.get('source_id', ''))
    return (value.get('message_time', 0), int(source) if source.isdigit() else value.get('sequence',0), value.get('applied_at',0))


def paused(state, item):
    import conversation as c
    # Incoming messages carry their actual author. Replying to an owner's
    # comment never grants that third-party author the owner's exemption.
    # Outgoing submissions have no author field; bind those to the verified
    # owner source comment they are replying to instead.
    if c.is_owner(item) or ('uid' not in item and owner_target(state, item, item.get('parent'))):
        return None
    controls = state.get('reply_controls', {})
    applicable = [controls[key] for key in ('*', resource_key(item)) if key in controls]
    latest = max(applicable, key=event_order) if applicable else None
    return latest if latest and latest.get('paused') else None


def receipt(row):
    return {'success': False, 'reply_paused': True, 'skip_target': True,
            'continue_queue': True, 'submitted': False,
            'resource_key': row['resource_key'],
            'error': '主人已暂停此评论区回复；尚未发送，继续处理其他评论区。'}


def paused_keys(state):
    result=[]
    for key,row in state.get('reply_controls',{}).items():
        if key=='*':
            if row.get('paused'):result.append(key)
        else:
            type_,oid=key.split(':',1)
            if paused(state,{'comment_type':type_,'oid':oid}):result.append(key)
    return result


def require_allowed(oid, type_, text=None, root=None, parent=None):
    import conversation as c
    type_ = getattr(type_, 'value', type_)
    with c.locked_state() as state:
        row = paused(state, {'oid': oid, 'comment_type': type_, 'parent': parent})
        grant = _ack.get()
        if grant:
            event = state.get('reply_control_events', {}).get(grant[0], {})
            ack = event.get('acknowledgement', {})
            origin = event.get('incoming', {})
            origin = event.get('ack_target') or origin
            current = state.get('reply_controls', {}).get(event.get('resource_key'), {})
            valid = (ack.get('token') == grant[1] and ack.get('status') == 'sending'
                     and current.get('source_id') == event.get('source_id')
                     and resource_key(origin) == resource_key({'oid':oid,'comment_type':type_})
                     and text == ack.get('text') and str(parent or '') == str(origin.get('rpid') or '')
                     and str(root or '') == str(origin.get('root') or origin.get('rpid') or ''))
            if not valid:
                raise ReplyPaused('暂停确认的提交范围不匹配，尚未发送')
            return
    if row:
        raise ReplyPaused('主人已暂停此评论区回复，尚未提交')


def intent(text):
    """Only direct controls with an explicit public-reply scope use the fast path.

    Quoted, hypothetical and ambiguous language remains ordinary conversation or
    goes to the existing generic owner agent; it cannot silently change controls.
    """
    text = unicodedata.normalize('NFKC', str(text or '')).strip()
    if re.search(r'(?:为什么|怎么|是否|是不是|有没有).{0,12}(?:暂停|停止|恢复|关闭)|(?:暂停|停止|恢复|关闭).{0,30}(?:了吗|了没|了么|了没有)[？?。!！\s]*$|(?:没有让|没让|不是让).{0,8}(?:暂停|停止|恢复|关闭)', text):
        return None
    if re.search(r'[“”「」『』"\n]|如果|假如|假设|比如|例如|他说|她说|有人说|什么意思|怎么说|能不能别|不要只|别只|不要(?:暂停|停止)|别(?:暂停|停止)|不要恢复|别恢复', text):
        return None
    scope = r'(?:这个|该|此|这条|本条|当前|整个)?\s*(?:评论区|视频(?:的)?(?:评论|回复)?|动态(?:的)?(?:评论|回复)?|帖子(?:的)?(?:评论|回复)?|这里|这儿|这底下)'
    resume = r'(?:恢复|继续|重新开启|重新打开|取消暂停|解除暂停|允许)'
    stop = r'(?:别(?:再)?|不要(?:再)?|不用(?:再)?|先别|停止|暂停|关闭|取消)'
    reply = r'(?:自动)?(?:回(?:复)?|接话|评论)'
    global_scope = bool(re.search(r'(?:所有|全部|全站).{0,6}(?:评论|回复)', text))
    scoped = bool(re.search(scope, text)) or global_scope
    if not scoped:
        return None
    if re.search(resume + r'.{0,18}' + reply, text) or re.search(reply + r'.{0,10}' + resume, text):
        return ('resume', '*' if global_scope else 'resource')
    if re.search(stop + r'.{0,18}' + reply, text) or re.search(reply + r'.{0,10}(?:暂停|停止|关闭)', text):
        return ('pause', '*' if global_scope else 'resource')
    return None


def apply(state, incoming, action, scope='resource', target=None):
    import conversation as c
    if not c.is_owner(incoming):
        raise PermissionError('只有真实主人来信可以修改回复控制')
    remember_owner(state, incoming)
    if action not in ('pause', 'resume') or scope not in ('resource', '*'):
        raise ValueError('Invalid reply control')
    key = '*' if scope == '*' else resource_key(target or incoming)
    if not key:
        raise ValueError('来信没有已核验的评论区，不能猜测暂停范围')
    source = str(incoming.get('rpid') or incoming.get('id') or incoming.get('message_id') or '')
    if not source:
        raise ValueError('Missing authenticated source')
    events = state.setdefault('reply_control_events', {})
    event_id = source + '@' + key
    existing = events.get(event_id) or events.get(source)
    if existing and existing.get('resource_key') == key:
        return existing
    row = {'resource_key': key, 'paused': action == 'pause', 'source_id': source,
           'uid': str(incoming['uid']), 'text': incoming['text'],
           'message_time': float(incoming.get('time') or 0), 'applied_at': time.time(),
           'event_id': event_id, 'action': action, 'incoming': dict(incoming),'sequence':incoming.get('seqno',0)}
    current = state.setdefault('reply_controls', {}).get(key, {})
    # Pagination can deliver older messages after newer ones. Receipt order must
    # never resurrect a stale pause or undo a newer owner instruction.
    row['effective'] = not current or event_order(row) > event_order(current)
    if incoming.get('rpid') and resource_key(incoming) and (target is None or resource_key(target) == resource_key(incoming)):
        row['acknowledgement'] = {'status': 'waiting', 'text': ACK_TEXT[action].replace('这个评论区','所有评论区') if key=='*' else ACK_TEXT[action]}
    events[event_id] = row
    if row['effective']:
        state['reply_controls'][key] = dict(row)
    return row


def consume(state, incoming):
    import conversation as c
    if not c.is_owner(incoming):
        return False
    remember_owner(state, incoming)
    control = intent(incoming.get('text'))
    if not control:
        return False
    if not incoming.get('time'):
        return False
    if control[1] != '*' and not resource_key(incoming):
        return False
    # An explicit different URL/BV is a target to resolve with the existing
    # agent tools, never a reason to pause whichever page delivered the message.
    ids = re.findall(r'BV[0-9A-Za-z]{10}', incoming.get('text', ''))
    if ids and any(value != incoming.get('bvid') for value in ids):
        return False
    if re.search(r'https?://', incoming.get('text', '')):
        return False
    row = apply(state, incoming, *control)
    source = str(incoming.get('rpid') or incoming.get('id'))
    state['pending'].pop(source, None)
    state['handled'].setdefault(source, {'status': 'reply_control', 'resource_key': row['resource_key'],
                                       'action': control[0], 'day': c.today(), 'owner': True})
    return True


async def acknowledge(credential):
    """One short code-generated acknowledgement; unknown receipts never resend.

    A durable pause takes effect before this function runs. Its narrowly bound
    capability permits only the saved confirmation on the actual owner message.
    """
    import conversation as c
    from bilibili_api import comment
    from bilibili_api.exceptions import ResponseCodeException
    if c.budget_stop_reason():
        return
    chosen = None
    with c.locked_state() as state:
        for event_id, event in state.get('reply_control_events', {}).items():
            ack = event.get('acknowledgement', {})
            if ack.get('status') == 'reserved' and ack.get('started_at', 0) < time.time() - 60:
                ack.update(status='waiting')
            if ack.get('status') == 'sending' and ack.get('started_at', 0) < time.time() - 120:
                ack.update(status='uncertain', error='确认发送时中断；保留回执，不自动重复')
            if ack.get('status') != 'waiting':
                continue
            current = state['reply_controls'].get(event['resource_key'], {})
            if current.get('source_id') != event['source_id'] or not event.get('effective'):
                ack['status'] = 'superseded'
                continue
            due = max(time.time(), state.get('last_scheduled_at', 0) + c.MIN_SEND_INTERVAL,
                      state.get('last_sent_at', 0) + c.MIN_SEND_INTERVAL)
            if due > time.time() + 30:
                break
            ack.update(status='reserved', token=uuid.uuid4().hex, started_at=time.time(), pid=os.getpid())
            state['last_scheduled_at'] = due
            chosen = (event_id, dict(event), dict(ack), due)
            break
    if not chosen:
        return
    event_id, event, ack, due = chosen
    origin = event.get('ack_target') or event['incoming']
    await asyncio.sleep(max(0, due - time.time()))
    with c.locked_state() as state:
        saved = state['reply_control_events'][event_id]['acknowledgement']
        current = state['reply_controls'].get(event['resource_key'], {})
        if saved.get('token') != ack['token']:
            return
        if current.get('source_id') != event['source_id']:
            saved['status'] = 'superseded'
            return
        if c.budget_stop_reason():
            saved['status'] = 'waiting'
            return
        saved['status'] = 'sending'
    grant = _ack.set((event_id, ack['token']))
    try:
        from reply_publish import send_comment_native
        result = await asyncio.wait_for(send_comment_native(ack['text'], int(origin.get('oid') or origin['aid']),
            comment.CommentResourceType(int(origin.get('comment_type', 1))), credential,
            root=int(origin.get('root') or origin['rpid']) if origin.get('rpid') else None,
            parent=int(origin['rpid']) if origin.get('rpid') else None,
            sync_to_dynamic=c.read_json(c.CONFIG_FILE, {}).get('mirrorDynamics') is True), timeout=30)
        rpid = str(result.get('rpid_str') or result.get('rpid') or (result.get('reply') or {}).get('rpid') or '')
        if not rpid.isdigit():
            raise RuntimeError('确认回复没有明确评论ID')
        with c.locked_state() as state:
            state['reply_control_events'][event_id]['acknowledgement'].update(status='success', rpid=rpid, finished_at=time.time())
            if origin.get('rpid') and str(origin['rpid']) in state['handled']:
                state['handled'][str(origin['rpid'])]['reply'] = rpid
            actual_root = str(origin.get('root') or origin.get('rpid') or rpid)
            state['sent'][rpid] = actual_root
            state['roots'].setdefault(actual_root, {'bvid':origin.get('bvid', ''),
                'aid':origin.get('oid') or origin.get('aid'), 'oid':str(origin.get('oid') or origin.get('aid')),
                'comment_type':int(origin.get('comment_type', 1)), 'created_at':int(time.time()),
                'owned':not bool(origin.get('rpid'))})
            state['last_sent_at'] = time.time()
            c.day_counts(state)['owner_replies'] += 1
            state['recent_public'] = (state.get('recent_public', []) + [{'rpid':rpid,
                'bvid':origin.get('bvid',''), 'oid':str(origin.get('oid') or origin.get('aid')),
                'comment_type':int(origin.get('comment_type',1)), 'text':ack['text'], 'control_ack':True}])[-20:]
    except (ReplyPaused, ResponseCodeException) as error:
        with c.locked_state() as state:
            state['reply_control_events'][event_id]['acknowledgement'].update(status='failed', submitted=False,
                error=type(error).__name__, finished_at=time.time())
    except Exception as error:
        with c.locked_state() as state:
            state['reply_control_events'][event_id]['acknowledgement'].update(status='uncertain',
                error=type(error).__name__, finished_at=time.time())
    finally:
        _ack.reset(grant)


async def owner_control(credential, action, bvid='', resource=''):
    """Generic agent control: targets must be read and verified, no guessed IDs."""
    import conversation as c
    import owner_tasks as tasks
    if bvid and resource:
        raise ValueError('每次只指定一个已核验的视频或动态目标')
    with c.locked_state() as state:
        task, incoming = tasks.authorize(state)
        origin = dict(incoming)
        if resource:
            res = task.get('resources', {}).get(resource)
            if not res:
                raise PermissionError('先实际读取目标视频或动态，不能猜暂停范围')
            target = {'oid':res['oid'], 'comment_type':res['type']}
        elif bvid:
            target = None
        elif task.get('channel') == 'comment':
            target = origin
        else:
            raise ValueError('请指定要暂停的视频或动态')
    if bvid:
        from bilibili_api import video
        info = await video.Video(bvid=bvid, credential=credential).get_info()
        if info.get('bvid') != bvid:
            raise ValueError('视频身份不匹配')
        target = {'oid':str(info['aid']), 'comment_type':1, 'bvid':bvid}
    key = resource_key(target)
    if not key:
        raise ValueError('Missing verified resource')
    with c.locked_state() as state:
        tasks.authorize(state)
        # Cross-resource/DM controls confirm as a new target comment, without
        # borrowing a parent ID from the original, different comment section.
        row = apply(state, origin, action, target=target)
        if resource_key(origin) != key or not origin.get('rpid'):
            row['ack_target'] = {**target, 'rpid':'', 'root':''}
            row.setdefault('acknowledgement', {'status':'waiting','text':ACK_TEXT[action]})
        task = state['owner_tasks'][task['id']]
        type_,oid=key.split(':',1)
        actual_paused = bool(paused(state,{'comment_type':type_,'oid':oid}))
        effective = row['effective'] and state['reply_controls'][key]['source_id'] == row['source_id']
        task['reply_control_result'] = {'resource_key':key,'paused':actual_paused,'source_id':row['source_id'],
                                      'event_id':row.get('event_id',row['source_id'])}
        task['actions']['reply-control:'+key] = {'status':'success','success':True,
            'params':{'action':action+'_replies','resource_key':key},
            'paused':actual_paused,'at':time.time(),'local_control':True,'superseded':not effective}
    return {'success':True,'resource_key':key,'paused':actual_paused,'superseded':not effective,
            'acknowledgement_status':row.get('acknowledgement',{}).get('status'),
            'message':'已保存回复控制，重启后仍有效；主人的消息照常处理。' if effective else '更晚收到的主人指令优先，本条未覆盖当前控制。'}
