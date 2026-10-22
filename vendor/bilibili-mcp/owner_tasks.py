"""Authenticated, opt-in owner commands. Queue and receipts share the conversation lock."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path

from bilibili_api.exceptions import ResponseCodeException
import conversation as c
import private_messages as dm

PREFIX = '主人命令：'


def enabled():
    return c.read_json(c.CONFIG_FILE, {}).get('ownerTaskEnabled') is True


def is_command(item):
    return request_scope(item) is not None


def command_text(item):
    text = str(item.get('text') or '')
    if text.startswith(PREFIX):return text[len(PREFIX):]
    if item.get('kind')=='owner_mention':
        for name in item.get('addressed_names',[]):
            text=re.sub(r'^\s*@'+re.escape(name)+r'\s*', '',text)
        if not item.get('addressed_names'):
            text=re.sub(r'^\s*@[^\s:：]+\s*[:：]?\s*','',text)
    return text


def wants_video(item):
    text = command_text(item)
    if re.search(r'动态|图文|/opus/|t\.bilibili\.com',text) and not re.search(r'视频|BV[0-9A-Za-z]{10}',text):
        return False
    if re.fullmatch(r'\s*(?:(?:请|帮我|去|在|给)\s*)*BV[0-9A-Za-z]{10}(?:视频)?(?:发一条评论|评论|回复)[：:].+', text, re.S):
        return False
    if re.search(r'只(?:点赞|投币|收藏|关注)|不用看|不要看|不必看', text):
        return False
    # Completed/attributive viewing describes a selection pool, not a new watch.
    # Keep the remaining clause so "从看过的视频里选一个重新看" still requests viewing.
    directive=re.sub(r'(?:观看|浏览|看|刷)(?:过|完|了)*(?=的)|(?:观看|浏览|看|刷)(?:过|了)', '', text)
    viewing=bool(re.search(r'去看|观看|浏览|看看|看一下|看下|看完|重看|再看|看一[遍次]|刷.{0,4}视频|看这个视频|看这[个条段]', directive))
    media=bool(re.search(r'视频|BV[0-9A-Za-z]{10}|https?://(?:www\.)?bilibili\.com/video/|https?://b23\.tv/|《[^》]+》',text))
    current_video=bool(item.get('bvid') and re.fullmatch(r'\s*(?:你|请|帮我|去|来|先|再|一下|[，,\s])*(?:看看|看一下|看下|观看)(?:这[个段条]|那[个段条])?[。！!\s]*',text))
    if viewing and (media or current_video):
        return True
    bare_mention=item.get('kind')=='owner_mention' and item.get('mention_mode')!='conversation' and not text.strip()
    return bare_mention or bool(re.fullmatch(r'\s*(?:https?://[^\s\u3000-\u9fff，。！？]+|BV[0-9A-Za-z]{10}|《[^》]+》)\s*', text))

def _actual_media_proof(data, bvid):
    """Only actual audio/visual results count; titles, subtitles and sampler logs do not."""
    if not isinstance(data,dict) or data.get('ok') is not True or data.get('bvid')!=bvid:
        return None
    if data.get('source') not in ('actual_video_and_audio','actual_sampled_video_and_audio'):
        return None
    analysis=data.get('analysis')
    if not isinstance(analysis,dict):return None
    visual=analysis.get('visual_observations')
    audio=analysis.get('audio_observations')
    # A genuinely silent video can have no audio observations.
    if not isinstance(visual,list) or not any(isinstance(v,dict) and v.get('observation') for v in visual) or not isinstance(audio,list):
        return None
    at=data.get('analysed_at')
    if isinstance(at,bool) or not isinstance(at,(int,float)) or not 0<at<=time.time()+60:
        return None
    return {'ok':True,'source':data['source'],'analysed_at':at,'validated':True}


def video_evidence(task, bvid, allow_history=False):
    """Use task-bound evidence for fresh viewing; validated durable evidence for history."""
    if not re.fullmatch(r'BV[0-9A-Za-z]{10}',str(bvid)):return None
    current=task.get('media',{}).get(bvid,{})
    if current.get('ok') is True and current.get('validated') is True and current.get('source') in ('actual_video_and_audio','actual_sampled_video_and_audio'):
        return current
    if not allow_history or task.get('media_failures',{}).get(bvid):return None
    # The conversation file selects the account home, including isolated test profiles.
    try:files=(c.STATE_FILE.parent/'video-cache').glob(bvid+'-*.json')
    except OSError:return None
    best=None
    try:
        for file in files:
            try:proof=_actual_media_proof(json.loads(file.read_text('utf-8')),bvid)
            except (OSError,ValueError,TypeError):continue
            if proof and (not best or proof['analysed_at']>best['analysed_at']):
                best={**proof,'origin':'video-cache','file':file.name}
    except OSError:pass
    return best


def verify_video_completion(task, incoming, text):
    watching=wants_video(incoming)
    claimed=bool(re.search(r'看完|已经看过|已经看了',text or ''))
    if not (watching or claimed):return {'required':False,'ok':True}
    targets=set(re.findall(r'BV[0-9A-Za-z]{10}',command_text(incoming)+'\n'+(text or '')))
    # A notification's source video is only a target for an actual viewing request.
    if not targets and watching and incoming.get('kind')=='owner_mention' and incoming.get('bvid'):
        targets.add(incoming['bvid'])
    if not targets:
        targets.update(r.get('params',{}).get('bvid') for r in task.get('actions',{}).values()
                       if r.get('status')=='success' and r.get('params',{}).get('bvid'))
    if not targets:targets.update(task.get('media',{}))
    proofs={bvid:proof for bvid in targets if (proof:=video_evidence(task,bvid,allow_history=not watching))}
    return {'required':True,'watch_requested':watching,'ok':bool(targets) and targets.issubset(proofs),
            'targets':sorted(targets),'proofs':proofs,'missing':sorted(targets-proofs.keys())}


def image_delivery_requirement(state, task, incoming):
    """Resolve a short continuation only from earlier actual owner messages."""
    text = command_text(incoming).strip()
    # Preserve the original deliverable across continuations, rather than only
    # recognizing one short phrase against a rolling chat history.
    parent_id=task.get('parent_task_id');seen=set()
    while parent_id and parent_id not in seen and len(seen)<4:
        seen.add(parent_id);parent=state.get('owner_tasks',{}).get(parent_id,{})
        if parent.get('uid')!=c.OWNER_UID:break
        text=str(parent.get('command') or '')+'\n'+text
        parent_id=parent.get('parent_task_id')
    if re.fullmatch(r'(?:在|给)?BV[0-9A-Za-z]{10}(?:视频)?(?:发一条评论|评论|回复)[：:].+', text, re.S):
        return {'required': False, 'image_only': False, 'channel': None}
    if task.get('channel') == 'dm' and len(text) <= 40 and re.search(r'现在|继续|去发|发一个|发没|只发|重发|试试', text):
        history = dm.section(state)['history'].get(c.OWNER_UID, [])
        end = next((i for i, row in enumerate(history) if row.get('message_id') == task['source_id']), 0)
        previous = [row.get('text', '') for row in history[max(0, end-8):end] if row.get('role') == 'incoming']
        for earlier in reversed(previous):
            if re.search(r'表情包|表情图|那张图|这张图|图片', earlier) and re.search(r'发|贴|用|评论', earlier):
                text = earlier + '\n' + text
                break
    media=r'(?:表情包|表情图|那张图|这张图|图片|截图|截屏|照片|相片|图像)'
    required = bool(re.search(r'(?:发|贴|用|评论|附|带|上传|晒).{0,160}'+media+'|'+media+r'.{0,100}(?:发|贴|评论|附|上传)', text, re.S))
    required = required or bool(re.search(r'\.(?:png|jpe?g|webp|gif)\b',text,re.I) and re.search(r'发|传|贴',text))
    if re.search(r'不要(?:发|用|带).{0,8}(?:图片|表情包|表情图)', text):
        required = False
    with_caption = bool(re.search(r'配文|配上文字|加一句|加一段|图文|图片和文字|图和文字|附.{0,3}文案|(?:并且?|同时|再).{0,6}(?:回复|回应|评论|说)', text))
    private = bool(re.search(r'发给我|发送给我|发我|传给我|私信(?:给我)?|发给主人|发送给主人', text))
    public = bool(re.search(r'评论区|评论里|底下|视频下|发.{0,12}评论|贴.{0,12}评论', text))
    public = public or bool(not private and re.search(r'动态|图文|opus/',text) and '评论' in text)
    destination = ('comment' if public else 'private' if private else
        'comment' if re.search(r'BV[0-9A-Za-z]{10}', text) else 'private' if task.get('channel')=='dm' else 'comment')
    result={'required': required, 'image_only': required and not with_caption, 'channel': destination if required else None}
    if required and re.search(r'截图|截屏',text):result['capture_required']=True
    return result


def comment_image_requirement(state, task, incoming):
    image = image_delivery_requirement(state, task, incoming)
    required = image['required'] and image['channel'] == 'comment'
    return {'required': required, 'image_only': required and image['image_only']}


def status_question(text):
    # A question about an earlier action is not a new request to repeat it.
    return bool(re.fullmatch(r'(?:你)?(?:已经|都|到底|还)?(?:关注|点赞|投币|收藏|评论|回复|观看|看|刷|做|完成|处理|发送|发)(?:完|好|过|成功|完成|了)*(?:没|没有|了吗|了么|了没有)(?:[，,。？！?!！\s]*(?:赶紧的|赶紧|快点|快点啊|啊|呀|呢|嘛))*[，,。？！?!！\s]*',str(text).strip()))


def request_scope(item):
    if not enabled() or not c.is_owner(item):
        return None
    text = command_text(item).strip()
    if str(item.get('text') or '').startswith(PREFIX):
        return 'full'
    if re.fullmatch(r'(?:确认|同意|批准)\s*[a-f0-9]{8}[。！!\s]*', text):
        return 'confirmation'
    if re.search(r'不要执行|不用执行|别执行|只是举例|只是引用|^(?:有人说|别人说|他说|她说|比如|举例|例如|假如|如果我说)', text):
        return None
    if re.fullmatch(r'[“"「].*[”"」][。\s]*', text) and re.search(r'点赞|投币|收藏|关注|去看', text):
        return None
    # A real video @ notification supplies its authenticated current-video target.
    if item.get('kind') == 'owner_mention' and wants_video(item) and re.fullmatch(r'BV[0-9A-Za-z]{10}', str(item.get('bvid') or '')):
        return 'bili'
    if item.get('owner_agent_delegated') is True:
        return 'full'
    if item.get('bili_delegated') is True:
        return 'bili'
    if status_question(text):
        return None
    from reply_controls import intent
    if intent(text):
        return 'full'
    if re.search(r'^(?:不用|不要|别|不必).{0,8}(?:观看|看视频|看这个|点赞|投币|收藏|关注)', text) and not re.search(r'只(?:看|点赞|投币|收藏)|但是|但请|改为', text):
        return None
    if re.search(r'BV[0-9A-Za-z]{10}|(?i:\bav\d+\b)|https?://(?:[\w-]+\.)?bilibili\.com/|https?://b23\.tv/', text):
        return 'bili'
    # Named videos and ordinary imperatives do not need the general computer-task prefix.
    if re.search(r'^(?:视频(?:名(?:称)?|标题)?\s*[:：]|《[^》]+》[。\s]*$)', text):
        return 'bili'
    lead = re.sub(r'^(?:(?:小肥鱼|大肥鱼|你|请你|请|麻烦你?|帮我|给我|可以|能不能|能否|去|先|顺便|再)[，,\s]*)+', '', text)
    if wants_video(item) and re.search(r'^(?:看看|看一下|看下|观看|播放)',lead):return 'bili'
    if re.search(r'^(?:刷(?:个|一下|一[个条轮])?视频|搜索.{0,12}视频|搜(?:一下|下|个)?视频|点赞|点个赞|点一下赞|赞一下|投币|投[一二两12]个?币|收藏|关注|评论|回复.{0,8}评论|三连|一键三连)', lead):
        return 'bili'
    return None


def jobs(state):
    return state.setdefault('owner_tasks', {})


def source(state, task):
    queue = dm.section(state)['pending'] if task['channel'] == 'dm' else state['pending']
    return queue.get(task['source_id'])


def authorize(state, task_id=None, token=None):
    task_id = task_id or os.environ.get('BILI_OWNER_TASK_ID', '')
    token = token or os.environ.get('BILI_OWNER_TASK_TOKEN', '')
    task = jobs(state).get(task_id)
    incoming = source(state, task) if task else None
    if (not task or not token or not hmac.compare_digest(str(task.get('token', '')), str(token))
            or task.get('status') != 'running' or not incoming or not is_command(incoming)
            or incoming.get('blocked_reason')):
        raise PermissionError('没有对应主账号的有效命令，或任务已处理/撤回；不能执行')
    return task, incoming


def require_full(state):
    task, incoming = authorize(state)
    if task.get('scope', 'full') != 'full':
        raise PermissionError('本轮为代码预设流程，不能扩大操作范围；其他明确任务由智能体执行')
    return task, incoming


def delegate(source_id, channel='dm', scope='full', domain='general',parent_task_id=''):
    if not enabled() or channel not in ('dm', 'comment'):
        return {'success': False}
    with c.locked_state() as state:
        queue = dm.section(state)['pending'] if channel == 'dm' else state['pending']
        incoming = queue.get(str(source_id))
        if not incoming or not c.is_owner(incoming) or incoming.get('blocked_reason'):
            return {'success': False, 'error': '只接受真实主账号的当前待处理消息'}
        if scope not in ('bili', 'full'):
            return {'success': False, 'error': '无效任务范围'}
        if domain not in ('bilibili','computer','general'):
            return {'success':False,'error':'无效任务领域'}
        text = str(incoming.get('text') or '').strip()
        if re.search(r'不要执行|不用执行|别执行|只是举例|只是引用|^(?:有人说|别人说|他说|她说|比如|举例|例如|假如|如果我说|不要|不用|别)', text):
            return {'success': False, 'error': '否定、引文或举例不是当前执行请求'}
        if re.fullmatch(r'[“"「].*[”"」][。\s]*', text):
            return {'success': False, 'error': '不能把引文当作当前执行请求'}
        if parent_task_id:
            parent=jobs(state).get(str(parent_task_id))
            if not parent or str(parent.get('uid'))!=c.OWNER_UID or parent.get('source_id')==str(source_id):
                return {'success':False,'error':'只能关联真实主人已有的另一条任务作为上下文'}
            incoming['parent_task_id']=str(parent_task_id)
        incoming['bili_delegated'] = scope == 'bili'
        incoming['owner_agent_delegated'] = scope == 'full'
        incoming['owner_task_domain'] = domain
        return {'success': True, 'scope': request_scope(incoming), 'message': '已转交主人任务路由，代码预设优先，其他目标由智能体执行；当前聊天不要代答完成'}


async def resolve_video(target):
    with c.locked_state() as state:
        authorize(state)
    text = str(target).strip()
    found = re.search(r'BV[0-9A-Za-z]{10}', text)
    if found:
        return {'ok': True, 'bvid': found.group()}
    found = re.search(r'(?i)\bav(\d+)\b', text)
    if found:
        from bilibili_api import video
        return {'ok': True, 'bvid': video.Video(aid=int(found.group(1))).get_bvid()}
    link = re.search(r'https?://[^\s<>“”]+', text)
    if not link:
        return {'ok': False, 'search_title': text, 'message': '视频名称用bili_search检索；重名时核对UP主和标题，不猜目标'}
    from urllib.parse import urlparse, urljoin
    import httpx
    def allowed(url):
        p = urlparse(url)
        return p.scheme in ('http', 'https') and not p.username and not p.password and p.port in (None, 80, 443) and (p.hostname == 'b23.tv' or p.hostname == 'bilibili.com' or str(p.hostname).endswith('.bilibili.com'))
    url = link.group().rstrip('。！，,；;）)')
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            if not allowed(url):
                return {'ok': False, 'error': '仅解析B站视频链接，不能跳转到外部网址'}
            async with client.stream('GET', url) as response:
                next_url = response.headers.get('location')
                if response.status_code in (301,302,303,307,308) and next_url:
                    url = urljoin(url, next_url)
                    continue
                found = re.search(r'BV[0-9A-Za-z]{10}', url)
                return {'ok': bool(found), 'bvid': found.group() if found else None, 'url': url}
    return {'ok': False, 'error': '视频链接重定向过多，请给主人说明并索取完整链接或名称'}


def record_tool(name, value=None, task_id=None, token=None):
    with c.locked_state() as state:
        task, _ = authorize(state, task_id, token)
        task['executed_mcp'] = (task.get('executed_mcp', []) + [name])[-150:]
        stage={'bili_owner_context':'正在核对任务和上下文','bili_owner_comments':'已读取目标楼层，正在组织回复',
            'bili_owner_action':'正在核对实际操作回执','bili_owner_browser':'正在执行网页操作','bili_owner_result':'正在提交最终结果'}.get(name)
        if stage:task['progress']={'stage':stage,'at':time.time()}
        if name == 'bili_video_understand':
            try:
                data = json.loads(value) if isinstance(value, str) else value
                proof=_actual_media_proof(data,data.get('bvid'))
                if proof and data.get('bvid'):
                    task.setdefault('media', {})[data['bvid']] = {**proof,'recorded_at':time.time()}
                    task.setdefault('media_failures',{}).pop(data['bvid'],None)
                elif data.get('bvid'):
                    task.setdefault('media',{}).pop(data['bvid'],None)
                    task.setdefault('media_failures', {})[data['bvid']] = True
            except (ValueError, TypeError, AttributeError):
                pass


async def dispatch(credential, mode='claim', task_id='', token='', trace='[]', cli_ok=False, host_pid=0):
    """Host-only dispatch. This tool is excluded from every model's whitelist."""
    if not enabled():
        return {'enabled': False}
    if mode == 'claim':
        if c.budget_stop_reason():
            return {'budget_stopped': True}
        # A host process that vanished cannot keep the command slot for 45 min.
        # This lease is renewed by the host, independent of model/tool progress.
        with c.locked_state() as state:
            expired=[(t['id'],t.get('token','')) for t in jobs(state).values()
                if t.get('status')=='running' and t.get('host_pid')
                and time.time()-t.get('heartbeat_at',t.get('claimed_at',0))>150]
        for identifier,secret in expired:
            await dispatch(credential,'recover',identifier,secret)
        # Poll both channels even while the ordinary browsing worker is busy.
        await asyncio.gather(dm.poll(credential), c.pending_replies(credential))
        with c.locked_state() as state:
            active = [t for t in jobs(state).values() if t.get('status') == 'running']
            # A crashed worker cannot safely replay writes with unknown receipts.
            for task in active:
                if not task.get('host_pid') and time.time() - task.get('claimed_at', 0) > 45 * 60:
                    task.update(status='interrupted', finished_at=time.time())
                    incoming = source(state, task)
                    if incoming:
                        incoming['blocked_reason'] = '主人命令中断；保留记录，不自动重复可能已完成的操作'
                else:
                    return {'busy': True}
            candidates = [('dm', key, row) for key, row in dm.section(state)['pending'].items()]
            candidates += [('comment', key, row) for key, row in state['pending'].items()]
            for channel, key, item in sorted(candidates, key=lambda row: (row[2].get('time', 0), row[1])):
                if not is_command(item) or item.get('blocked_reason'):
                    continue
                identifier = hashlib.sha256((channel + ':' + key).encode()).hexdigest()[:24]
                if identifier in jobs(state):
                    existing=jobs(state)[identifier]
                    if existing.get('status')!='waiting' or existing.get('retry_at',0)>time.time():
                        continue
                    secret=uuid.uuid4().hex
                    existing.update(status='running',token=secret,heartbeat_at=time.time(),attempt_started_at=time.time())
                    if host_pid:existing['host_pid']=host_pid
                    return {'task_id':identifier,'token':secret,'channel':channel,'scope':existing['scope'],'route':existing['route'],'domain':existing.get('domain','general')}
                secret = uuid.uuid4().hex
                scope = request_scope(item)
                domain=item.get('owner_task_domain') or ('bilibili' if scope=='bili' else 'general')
                if re.search(r'桌面|电脑|本地|文件夹|文件路径|保存到|下载到',command_text(item)):
                    domain='computer'
                confirmation_id = None
                if scope == 'confirmation':
                    code = re.search(r'[a-f0-9]{8}', item['text']).group()
                    confirmation_id = next((t['id'] for t in jobs(state).values()
                        if t.get('status') == 'needs_confirmation' and t.get('confirmation', {}).get('code') == code
                        and not t.get('confirmation_used_by') and time.time() - t['confirmation']['created_at'] < 86400), None)
                    # A random confirmation number never grants general computer access.
                    scope = 'full' if confirmation_id else 'bili'
                from owner_presets import parse
                preset = parse(item) if not confirmation_id and scope != 'confirmation' else None
                route = 'preset' if preset else 'agent'
                # Only actual owner goals reach this queue. Unknown confirmation
                # codes remain a narrow rejection, never a computer authorization.
                unknown_confirmation = request_scope(item) == 'confirmation' and not confirmation_id
                if not preset and not unknown_confirmation:
                    scope = 'full'
                elif preset:
                    scope = 'bili'
                jobs(state)[identifier] = {'id': identifier, 'source_id': key, 'channel': channel,
                    'uid': c.OWNER_UID, 'status': 'running', 'token': secret,
                    'scope': scope, 'route': route, 'domain':domain,'confirmation_id': confirmation_id,
                    'command':command_text(item),'claimed_at': time.time(), 'actions': {}, 'artifacts': []}
                if item.get('parent_task_id'):jobs(state)[identifier]['parent_task_id']=item['parent_task_id']
                if isinstance(host_pid,int) and host_pid>0:
                    jobs(state)[identifier].update(host_pid=host_pid,heartbeat_at=time.time())
                if channel=='dm':
                    import copy
                    history=dm.section(state)['history'].get(c.OWNER_UID,[])
                    end=next((i+1 for i,row in enumerate(history) if row.get('message_id')==key),0)
                    before=item.get('context_before')
                    snapshot=(before+[{'role':'incoming','message_id':key,'text':item.get('text',''),'time':item.get('time')}]) if before is not None else history[max(0,end-12):end]
                    jobs(state)[identifier]['context_snapshot']=copy.deepcopy(snapshot)
                    jobs(state)[identifier]['image_refs']=copy.deepcopy(item.get('image_refs',[]))
                return {'task_id': identifier, 'token': secret, 'channel': channel, 'scope': scope, 'route': route,'domain':domain}
            return {'empty': True}
    if mode == 'preset':
        from owner_presets import run
        return await run(credential, task_id, token)
    if mode == 'heartbeat':
        with c.locked_state() as state:
            task=jobs(state).get(task_id)
            if task and token and hmac.compare_digest(str(task.get('token','')),str(token)):
                incoming=source(state,task)
                if not incoming or not is_command(incoming) or incoming.get('blocked_reason'):
                    return {'success':False,'cancelled':True}
            task,_=authorize(state,task_id,token)
            task['heartbeat_at']=time.time()
        return {'success':True}
    if mode == 'checkpoint':
        with c.locked_state() as state:
            task, _ = authorize(state, task_id, token)
            return {'success': True, 'result_staged': bool((task.get('proposed') or {}).get('text')),
                    'action_statuses': [r.get('status') for r in task['actions'].values()]}
    if mode == 'journal':
        with c.locked_state() as state:
            task, _ = authorize(state, task_id, token)
            entries = json.loads(trace)
            task['native_tools'] = entries[-100:]
        return {'success': True}
    if mode == 'defer':
        with c.locked_state() as state:
            task,_=authorize(state,task_id,token)
            uncertain_effects=bool(task.get('actions') or task.get('browser_run') or any(
                e.get('tool') not in ('read_file','view_file','list_directory','list_mcp_resources','get_mcp_server_schema') for e in task.get('native_tools',[])))
            if not uncertain_effects:
                attempts=task.setdefault('startup_attempts',[])
                attempts.append({'at':time.time(),'reason':'local_startup_unavailable'})
                task.update(status='waiting',retry_at=time.time()+min(300,30*2**min(len(attempts)-1,4)),progress={'stage':'暂时无法启动，保留任务等待恢复','at':time.time()})
                task.pop('token',None)
                return {'success':False,'deferred':True,'retry_at':task['retry_at']}
        return await dispatch(credential,'recover',task_id,token,trace,False)
    if mode == 'recover':
        # A dead worker must not leave a running row blocking later commands.
        with c.locked_state() as state:
            task = jobs(state).get(task_id)
            if not task or task.get('status') != 'running':
                return {'success': True, 'already_terminal': True}
            if not token or not hmac.compare_digest(str(task.get('token','')), str(token)):
                raise PermissionError('无效任务恢复凭据')
            incoming = source(state, task)
            task['worker_error'] = 'execution_exited_without_finalization'
            if not incoming or not is_command(incoming) or incoming.get('blocked_reason'):
                task.update(status='interrupted', finished_at=time.time())
                task.pop('token',None)
                return {'success':True,'status':'interrupted'}
            if not (task.get('proposed') or {}).get('text'):
                task['proposed'] = {'status':'failed','text':'主人，这条任务执行中断，还没有完成。已有操作记录已保留，后续消息会继续处理，不会重复提交。'}
        try:
            return await dispatch(credential,'finalize',task_id,token,trace,False)
        except Exception as error:
            with c.locked_state() as state:
                task=jobs(state)[task_id]
                task.update(status='delivery_failed',finished_at=time.time(),delivery={'success':False,'uncertain':True,'error':type(error).__name__})
                task.pop('token',None)
                incoming=source(state,task)
                if incoming:incoming['blocked_reason']='任务已结束，结果发送回执不明；保留记录待核查，不自动重发'
            return {'success':False,'status':'delivery_failed'}
    if mode != 'finalize':
        raise ValueError('Unknown owner dispatch mode')
    with c.locked_state() as state:
        task, incoming = authorize(state, task_id, token)
        proposed = task.get('proposed') or {}
        try:
            tools = json.loads(trace)
        except ValueError:
            tools = []
        actual = ([str(t)[:120] for t in tools if isinstance(t, str)] + task.get('executed_mcp', []))[-150:]
        task['executed_tools'] = actual
        task['cli_ok'] = bool(cli_ok)
        kind = proposed.get('status', 'failed')
        text = proposed.get('text')
        # A plain verbal promise or finish/context calls alone is not execution.
        evidence = [t for t in actual if t not in ('bili_owner_context', 'bili_owner_result',
                    'bili_owner_plan', 'bili_local_state', 'list_mcp_resources', 'get_mcp_server_schema')]
        successes = [r for r in task['actions'].values() if r.get('status') == 'success']
        image_request = image_delivery_requirement(state, task, incoming)
        claims_image = bool(re.search(r'(?:已|只|成功).{0,12}(?:发|贴).{0,35}(?:表情包|表情图|图片|纯图)', text or ''))
        destination = image_request['channel'] or ('private' if task['channel']=='dm' else 'comment')
        from owner_web import minimum_pictures
        browser_pictures=any(r.get('kind')=='comment' and r.get('success') and r.get('image_attached') and
            r.get('picture_count',0)>=minimum_pictures(command_text(incoming)) and str(r.get('rpid','')).isdigit()
            for r in (task.get('browser_run') or {}).get('receipts',[]))
        image_ok = any(r.get('image_sent') and r.get('params',{}).get('action')=='send_image' for r in successes) if destination=='private' else any(r.get('image_attached') for r in successes) or browser_pictures
        if kind=='completed' and task.get('media_reference_required'):
            generations=state.get('service_generations',{})
            valid_video=any(a.get('media',{}).get('kind')=='downloaded_video'
                and ((g:=generations.get(a['media'].get('source_generation'),{})).get('reference_evidence') or {}).get('verified')
                and int((g.get('reference_evidence') or {}).get('count',1))>=int(task.get('media_reference_count',1))
                and g.get('content_validation',{}).get('accepted') is not False
                for a in task.get('artifacts',[]))
            if not valid_video:
                kind,text='failed','主人，还没有核验到使用指定参考图的新视频成片；已有文件和记录保留，不能把外观不符的结果算完成。'
        if kind == 'completed' and (image_request['required'] or claims_image) and not image_ok:
            kind = 'failed'
            text = ('主人，这次只找到了图片，还没有核验到图片通过私信发给你，不能算发送完成；我保留了记录。' if destination=='private' else
                    '主人，这次还没有核验到图片附在评论中，不能算发图成功。已有记录已保留，我不会用文字代替图片重发。')
        if kind == 'completed' and not (evidence or successes or task.get('artifacts')):
            kind, text = 'failed', '主人，这次没有取得实际执行记录，不能算完成。请重新下达命令，我会继续查。'
        claims_bili_write=bool(re.search(r'(?:已|已经|成功).{0,45}(?:发布|发出|发好|回复|点赞|收藏|关注|投币|送达)',text or ''))
        bili_goal=task.get('domain')=='bilibili' or task.get('resources') or bool(re.search(r'B站|动态|UP主|评论区|楼层|BV[0-9A-Za-z]{10}',text or ''))
        web_receipts=(task.get('browser_run') or {}).get('receipts',[])
        if kind=='completed' and claims_bili_write and bili_goal and not (successes or any(r.get('success') for r in web_receipts)):
            kind,text='failed','主人，这次还没有取得实际发送或操作成功的记录，不能算已经完成；我会保留现有结果，先核查再继续。'
        if kind == 'completed' and any(r.get('status') != 'success' for r in task['actions'].values()):
            kind, text = 'failed', '主人，操作记录里仍有失败或回执不明的步骤，这次不能算全部完成。我保留了已有结果，没有重复提交。'
        follow_command = re.sub(r'^(?:(?:小肥鱼|大肥鱼|你|请你|请|麻烦你?|帮我|给我|可以|能不能|能否|去|先|顺便|再)[，,\s]*)+', '', command_text(incoming).strip())
        if (kind == 'completed' and follow_command.startswith('关注')
                and not any(r.get('params', {}).get('action') == 'follow' for r in successes)):
            kind, text = 'failed', '主人，这次还没有核验到关注成功，不能只凭搜索结果算完成。'
        if kind == 'completed' and task.get('browser_run') and not task['browser_run'].get('success'):
            kind, text = 'failed', '主人，网页任务还没有取得完成确认，我保留了现有记录；不会重复提交或付款，需先核查浏览器状态。'
        verification=verify_video_completion(task,incoming,text)
        if verification['required']:task['media_verification']=verification
        if kind == 'completed' and not verification['ok']:
            kind, text = 'failed', '主人，这次没有取得真实画面和音轨的理解结果，不能算已经看完视频；我保留了执行记录。'
        if not text:
            kind = 'failed'
            if not task['actions'] and not task.get('artifacts') and not task.get('browser_run'):
                text = '主人，这次没有取得完成结果，也没有提交B站操作，任务还没完成。'
            else:
                text = '主人，这次没有取得完整结果。已有操作记录已保存，需要先核查；我没有重复提交。'
        if time.time()-task.get('claimed_at',time.time())>120:
            text=re.sub(r'火速|马上就|立刻就','',text)
        task['result_status'] = kind
        task['result_text'] = text
        channel, source_id = task['channel'], task['source_id']
        public = {k: incoming.get(k) for k in ('bvid', 'rpid', 'root')}
        control_confirmation = None
        control_result = task.get('reply_control_result', {})
        control_event = state.get('reply_control_events', {}).get(control_result.get('event_id'), {})
        # The code-generated confirmation already owns this exact owner reply.
        # An agent final summary must not publish a second acknowledgement.
        if (channel == 'comment' and control_event.get('incoming', {}).get('rpid') == source_id
                and not control_event.get('ack_target') and control_event.get('acknowledgement')
                and task['actions'] and all(row.get('local_control') for row in task['actions'].values())):
            ack = control_event['acknowledgement']
            control_confirmation = {'success':True,'accepted':True,'control_acknowledgement':True,
                'acknowledgement_status':ack['status'],'rpid_str':ack.get('rpid'),
                'confirmation_pending':ack['status'] in ('waiting','reserved','sending'),
                'uncertain':ack['status']=='uncertain'}
    if control_confirmation is not None:
        result = control_confirmation
    elif channel == 'dm':
        result = await dm.send_guarded(source_id, text, credential, owner_task_id=task_id, owner_token=token)
    else:
        result = await c.send_guarded(public['bvid'], text, int(public['rpid']), int(public['root']),
                                      credential, owner_task_id=task_id, owner_token=token)
    with c.locked_state() as state:
        task = jobs(state)[task_id]
        if control_confirmation is not None:
            state['pending'].pop(source_id, None)
            state['handled'].setdefault(source_id, {'status':'reply_control','owner':True,
                'task_id':task_id,'resource_key':control_result['resource_key'],'day':c.today()})
        task.update(status=kind if result.get('success') else 'delivery_failed',
                    finished_at=time.time(), delivery=result)
        task.pop('token', None)
    if result.get('success'):
        try:
            from companion_state import access
            access('persona', mode='owner_reply', source_id=source_id, channel=channel, delta=0)
        except Exception:
            pass
    return {'success': result.get('success', False), 'status': kind, 'delivery': result}


def image_context(state, task):
    if task['channel'] != 'dm':
        return []
    private=dm.section(state)
    history=private['history'].get(c.OWNER_UID,[])
    end=next((i+1 for i,row in enumerate(history) if row.get('message_id')==task['source_id']),0)
    incoming=source(state,task) or {}
    cutoff=incoming.get('time',0)-1800
    result=[]
    rows=task.get('context_snapshot') or history[max(0,end-40):end]
    refs=task.get('image_refs') or incoming.get('image_refs') or []
    rows=list(rows)+[{'role':'incoming',**r} for r in refs]
    seen=set()
    for row in rows:
        if row.get('role')!='incoming' or row.get('time',0)<cutoff:
            continue
        identifier=row.get('message_id')
        if identifier in seen:continue
        seen.add(identifier)
        record=private.get('images',{}).get(identifier,{})
        pending=private['pending'].get(identifier,{})
        if not (record or row.get('image') or pending.get('message_type')==2 or '对方发来图片' in row.get('text','')):
            continue
        if private['handled'].get(identifier,{}).get('status')=='withdrawn':
            continue
        result.append({'message_id':identifier,'time':row.get('time'),'ok':bool(record.get('ok')),
            **{k:record[k] for k in ('description','visible_text','semantics','uncertainties') if k in record}})
    return result[-8:]


async def read_image(credential,message_id):
    with c.locked_state() as state:
        task,_=authorize(state)
        if str(message_id) not in {r['message_id'] for r in image_context(state,task)}:
            raise PermissionError('只能读取本条真实主人任务之前相关的图片来信')
        cached=dm.section(state).get('images',{}).get(str(message_id),{})
        if cached.get('ok'):
            return {k:v for k,v in cached.items() if k not in ('file','sha256')}
    from private_images import analyze
    result=await analyze(str(message_id),credential)
    with c.locked_state() as state:authorize(state)
    return {k:v for k,v in result.items() if k not in ('file','sha256')}


def context():
    from browser_services import authorizations
    from character_memory import catalog
    with c.locked_state() as state:
        task, incoming = authorize(state)
        history = dm.section(state)['history'].get(c.OWNER_UID, []) if task['channel'] == 'dm' else []
        # Later messages cannot silently become instructions for the current task.
        end = next((i + 1 for i, row in enumerate(history) if row.get('message_id') == task['source_id']), 0)
        confirmations = [{k: t.get(k) for k in ('id', 'confirmation', 'status')}
                         for t in jobs(state).values() if t.get('confirmation') and not t.get('confirmation_used_by')]
        related=sorted((t for t in jobs(state).values() if t['id']!=task['id'] and t.get('claimed_at',0)<task.get('claimed_at',0)),key=lambda t:(t['id']==task.get('parent_task_id'),t.get('claimed_at',0)),reverse=True)[:3]
        related=[{'task_id':t['id'],'command':t.get('command'),'status':t['status'],'plan':t.get('plan',[]),'resources':[{k:v for k,v in r.items() if k!='seen_comments'} for r in t.get('resources',{}).values()]+([(t.get('public_readback') or {})['resource']] if (t.get('public_readback') or {}).get('resource') else []),
            'receipts':[a for a in t.get('actions',{}).values() if a.get('status')=='success']} for t in related]
        return {'task_id': task['id'], 'uid': c.OWNER_UID, 'channel': task['channel'],'domain':task.get('domain','general'),
                'elapsed_seconds':round(max(0,time.time()-task.get('claimed_at',time.time()))),'progress':task.get('progress'),
                'scope': task.get('scope', 'full'), 'command': command_text(incoming), 'source': incoming,
                'recent_endings': c.reply_endings(state),
                'plan': task.get('plan', []),
                'comment_image_requirement': comment_image_requirement(state, task, incoming),
                'image_delivery_requirement': image_delivery_requirement(state, task, incoming),
                'image_comment_policy': c.read_json(c.STATE_FILE.parent/'comment-image-policy.json', {}),
                'service_authorizations': authorizations(),'character_memory':catalog(),
                'media_reference_requirement':{'required':bool(task.get('media_reference_required')),'count':task.get('media_reference_count',1)},
                'generation_receipts': [{'id':key,**{k:value.get(k) for k in ('task_id','status','credits','http_dispatched','reference_evidence','content_validation','artifact_sha256')}}
                    for key,value in state.get('service_generations',{}).items() if value.get('task_id') in (task['id'],task.get('parent_task_id'))],
                'context': task.get('context_snapshot') or history[max(0, end - 12):end], 'images':image_context(state,task), 'actions': task['actions'],
                'artifacts': task['artifacts'], 'native_tools': task.get('native_tools', []),
                'browser_run': task.get('browser_run'),
                'resources':task.get('resources',{}),'related_tasks':related,'parent_task_id':task.get('parent_task_id'),'authorized_followups':task.get('authorized_followups',[]),'previous_owner_focus':state.get('owner_focus'),
                'pending_confirmations': confirmations[-3:]}


def task_status(message_id):
    with c.locked_state() as state:
        incoming=dm.section(state)['pending'].get(str(message_id))
        if not incoming or not c.is_owner(incoming):
            raise PermissionError('只能向当前真实主人的私信提供其任务状态')
        requests={r.get('message_id'):r.get('text','') for r in dm.section(state)['history'].get(c.OWNER_UID,[]) if r.get('role')=='incoming'}
        # Progress enquiries are not failed new executions. Keep the actual work
        # and its target receipts visible even for older misrouted enquiries.
        tasks=sorted((t for t in jobs(state).values() if not status_question(t.get('command') or requests.get(t['source_id'],''))),key=lambda t:t.get('claimed_at',0),reverse=True)[:3]
        return {'tasks':[{'task_id':t['id'],'status':t['status'],'result_status':t.get('result_status'),
                         'request':t.get('command') or requests.get(t['source_id'],''),
                         'result_text':t.get('result_text'),
                         'artifacts_found':len(t.get('artifacts',[])),
                         'image_sent':any(a.get('image_sent') and a.get('status')=='success' for a in t.get('actions',{}).values()) or
                            any(r.get('kind')=='comment' and r.get('success') and r.get('image_attached') for r in (t.get('browser_run') or {}).get('receipts',[])),
                         'comment_picture_count':max((r.get('picture_count',0) for r in (t.get('browser_run') or {}).get('receipts',[]) if r.get('kind')=='comment' and r.get('success')),default=0),
                         'elapsed_seconds':round(max(0,(t.get('finished_at') or time.time())-t.get('claimed_at',time.time()))),
                         'progress':t.get('progress'),
                         'seconds_since_progress':round(max(0,time.time()-(t.get('progress') or {}).get('at',t.get('claimed_at',time.time())))),
                         'actions':[{'action':a.get('params',{}).get('action'),'status':a.get('status'),
                            **{k:a.get('params',{}).get(k) for k in ('uid','bvid','rpid') if a.get('params',{}).get(k)}} for a in t.get('actions',{}).values()],
                         'claimed_at':t.get('claimed_at'),'finished_at':t.get('finished_at')} for t in tasks],
                'instruction':'只报告真实状态与耗时。超过2分钟无新进展时直接说明尚未取得新进展，不以正在执行为不回复的借口，不承诺马上。找到图片不等于已发送；不透露内部工具或流程。'}


def plan(steps, task_id=None, token=None):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12 or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 200 for s in steps):
        raise ValueError('任务计划须为1–12个具体步骤，每步1–200字，包含目标核对与完成核验')
    with c.locked_state() as state:
        task, _ = authorize(state, task_id, token)
        task['plan'] = [s.strip() for s in steps]
        return {'success': True, 'plan': task['plan'], 'message': '计划已保存；计划不扩大原消息授权，也不代表操作完成'}


def propose(status, text, proposal_id='', details='', task_id=None, token=None):
    if status not in ('completed', 'needs_input', 'needs_confirmation', 'failed', 'cancelled'):
        raise ValueError('Invalid result status')
    text = str(text).strip()
    if c.contains_outbound_private_info(text):
        raise ValueError(c.OUTBOUND_PRIVACY_ERROR)
    if not 1 <= len(text) <= 150:
        raise ValueError('回执须为1–150字；长结果保存到主人指定文件，并简要回执')
    with c.locked_state() as state:
        task, incoming = authorize(state, task_id, token)
        if task['channel'] == 'comment' and re.search(r'(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|mnt)/)', text):
            raise ValueError('公开回执不能暴露本地路径；只简要报告结果')
        if status == 'needs_confirmation':
            if not details.strip():
                raise ValueError('花钱前须给出具体对象、动作、金额/币种与确认编号')
            code = task['id'][:8]
            if code not in text:
                raise ValueError(f'请在回执中包含确认编号{code}，请主人回复“确认 {code}”，不能泄露内部控制词')
            task['confirmation'] = {'code': code, 'details': details[:3000], 'created_at': time.time()}
        task['proposed'] = {'status': status, 'text': text}
        return {'success': True, 'staged': True, 'message': '结果已暂存；执行器核对工具记录后在原消息回复'}


def money_confirmation(code):
    with c.locked_state() as state:
        task, incoming = require_full(state)
        command = command_text(incoming).strip()
        # Explicit owner confirmation must itself use the command prefix and exact code.
        if not re.fullmatch(r'(?:确认|同意|批准)\s*' + re.escape(code) + r'[。！!\s]*', command):
            return {'success': False, 'error': '需主人另发“确认 <编号>”，不能把普通聊天当批准'}
        match = next((t for t in jobs(state).values()
                      if t.get('confirmation', {}).get('code') == code and t.get('status') == 'needs_confirmation'), None)
        if (not match or time.time() - match['confirmation']['created_at'] > 86400
                or match.get('confirmation_used_by') not in (None, task['id'])):
            return {'success': False, 'error': '确认已过期、已使用或不存在，需要重新列明金额确认'}
        match['confirmation_used_by'] = task['id']
        task['approved_money'] = match['confirmation']
        return {'success': True, 'approved': match['confirmation'], 'message': '仅限这笔已明确金额与对象的操作，不能扩展或加价'}


def artifact(path):
    with c.locked_state() as state:
        task, _ = require_full(state)
    target = Path(path)
    if not target.is_absolute() or not target.is_file():
        raise ValueError('产物必须是确实存在的绝对路径文件')
    # Never read credentials or service internals as an artifact.
    resolved = target.resolve()
    if any(x in ('.dsh', '.gemini', '.ssh', '.codex') for x in resolved.parts):
        raise PermissionError('运行数据或凭证目录不能作为交付产物')
    digest = hashlib.sha256()
    with resolved.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    receipt = {'path': str(resolved), 'bytes': resolved.stat().st_size, 'sha256': digest.hexdigest()}
    with c.locked_state() as state:
        task, _ = authorize(state)
        task['artifacts'].append(receipt)
    return {'success': True, **receipt}


POSTCONDITION_DELAYS=(0,0.3,0.8,1.5)


def relation_matches(data,desired):
    attribute=data.get('relation',{}).get('attribute') if isinstance(data,dict) else None
    return isinstance(attribute,int) and (attribute in (2,6))==desired


async def confirm_state(reader,predicate):
    value=None
    for delay in POSTCONDITION_DELAYS:
        if delay:await asyncio.sleep(delay)
        value=await reader()
        if predicate(value):return value
    return value


class CoinNotSubmitted(Exception):
    """The browser was stopped before any coin request left this process."""


def coin_request_matches(bvid,aid,number,page_url,form):
    from urllib.parse import urlsplit,parse_qs
    page=urlsplit(page_url);data=parse_qs(form)
    return (page.hostname=='www.bilibili.com' and page.path.rstrip('/')==f'/video/{bvid}' and
        data.get('aid')==[str(aid)] and (not data.get('bvid') or data['bvid']==[bvid]) and
        data.get('multiply')==[str(number)] and data.get('select_like',['0'])==['0'])


async def submit_coin(credential,bvid,number,*,expected_before=0,authorize_write=None):
    """Use the official composer once; validate the outgoing target atomically."""
    from playwright.async_api import async_playwright,TimeoutError as PlaywrightTimeout
    from urllib.parse import urlsplit
    credential.raise_for_no_sessdata();credential.raise_for_no_bili_jct()
    attempted=False;blocked=None;posts=0;stage='browser_start'
    try:
        async with async_playwright() as pw:
            executable=next((str(p) for p in [Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
                Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')] if p.is_file()),None)
            if not executable:raise CoinNotSubmitted('没有可用的Chrome或Edge，尚未投币')
            browser=await pw.chromium.launch(headless=True,executable_path=executable)
            try:
                context=await browser.new_context(locale='zh-CN',viewport={'width':1440,'height':1000})
                await context.add_cookies([{'name':k,'value':str(v),'domain':'.bilibili.com','path':'/','secure':True}
                    for k,v in credential.get_cookies().items() if v])
                stage='verify_login'
                nav=await (await context.request.get('https://api.bilibili.com/x/web-interface/nav')).json()
                if nav.get('code')!=0 or str(nav.get('data',{}).get('mid'))!=str(credential.dedeuserid):
                    raise CoinNotSubmitted('网页登录账号不匹配，尚未投币')
                info=await (await context.request.get('https://api.bilibili.com/x/web-interface/view',params={'bvid':bvid})).json()
                if info.get('code')!=0:raise CoinNotSubmitted('无法核对目标视频，尚未投币')
                aid=info['data']['aid'];page=await context.new_page()
                async def guard(route):
                    nonlocal attempted,blocked,posts
                    request=route.request
                    endpoint=urlsplit(request.url)
                    if (posts or request.method!='POST' or endpoint.scheme!='https' or endpoint.hostname!='api.bilibili.com' or
                        endpoint.path!='/x/web-interface/coin/add' or
                        not coin_request_matches(bvid,aid,number,page.url,request.post_data or '')):
                        blocked='视频、数量或附加动作已改变，已拦截投币请求'
                        return await route.abort()
                    try:
                        if authorize_write:authorize_write()
                        current=await (await context.request.get('https://api.bilibili.com/x/web-interface/archive/coins',params={'bvid':bvid})).json()
                        if current.get('code')!=0 or current.get('data',{}).get('multiply')!=expected_before:
                            raise ValueError('投币数量发生变化，先只读核对已有结果')
                    except Exception:
                        blocked='授权或当前投币状态变化，已在提交前停止'
                        return await route.abort()
                    posts+=1;attempted=True
                    await route.continue_()
                await context.route('**/x/web-interface/coin/add*',guard)
                stage='open_target'
                await page.goto(f'https://www.bilibili.com/video/{bvid}/',wait_until='domcontentloaded',timeout=45000)
                stage='wait_player_ready'
                await page.wait_for_function('document.querySelector("video")?.readyState>=1',timeout=20000)
                await page.locator('video').evaluate_all('(vs)=>vs.forEach(v=>{v.autoplay=false;v.pause()})')
                stage='open_composer'
                dialog=page.locator('.coin-operated-m-exp')
                # SSR renders the toolbar before its client click handlers are
                # hydrated. Re-opening a missing dialog is read-only; the final
                # confirmation below remains a single guarded submission.
                for _ in range(4):
                    if await dialog.is_visible():break
                    if urlsplit(page.url).path.rstrip('/')!=f'/video/{bvid}':raise CoinNotSubmitted('页面目标已切换，尚未投币')
                    await page.locator('.video-coin.video-toolbar-left-item').click(timeout=15000)
                    try:
                        await dialog.wait_for(state='visible',timeout=2000)
                        break
                    except PlaywrightTimeout:pass
                stage='select_quantity'
                await dialog.get_by_text(f'{number}硬币',exact=True).click(timeout=10000)
                checkbox=dialog.locator('.like-checkbox input[type="checkbox"]')
                stage='disable_extra_like'
                if await checkbox.is_checked():await dialog.locator('.like-checkbox label').click(timeout=10000)
                if await checkbox.is_checked():raise CoinNotSubmitted('无法取消附加点赞，尚未投币')
                stage='submit_once'
                async with page.expect_response(lambda r:r.request.method=='POST' and urlsplit(r.url).path=='/x/web-interface/coin/add',timeout=20000) as pending:
                    await dialog.get_by_text('确定',exact=True).click(timeout=10000)
                response=await pending.value;body=await response.json()
                if not isinstance(body,dict) or not isinstance(body.get('code'),int):raise ValueError('投币响应格式不明')
                if body['code']!=0:raise ResponseCodeException(body['code'],str(body.get('message') or body.get('msg') or '平台拒绝投币'))
                return {'transport':'official_web_composer','code':0,'aid':aid,'bvid':bvid,'coins_added':number,'additional_like':False}
            finally:await browser.close()
    except ResponseCodeException:raise
    except Exception as error:
        if not attempted:
            stopped=CoinNotSubmitted(blocked or '网页流程在投币提交前停止')
            reason=str(error)[:1500]
            for value in credential.get_cookies().values():
                if isinstance(value,str) and len(value)>=6:reason=reason.replace(value,'[已隐藏]')
            stopped.diagnostic={'stage':stage,'error_type':type(error).__name__,'reason':reason}
            raise stopped from error
        raise


def platform_error(error,credential):
    message=str(getattr(error,'msg','') or '')
    for value in credential.get_cookies().values():
        if isinstance(value,str) and len(value)>=6:message=message.replace(value,'[已隐藏]')
    message=re.sub(r'https?://\S+','[平台链接]',message)
    return {'success':False,'rejected':True,'uncertain':False,'platform_code':error.code,
        'platform_message':message[:240],'error':f'B站拒绝操作（{error.code}），不自动重试'}


async def reconcile_state(credential,record):
    """Read only. A delayed postcondition must never cause another submission."""
    from bilibili_api import video,user,favorite_list
    params=record['params'];kind=params['action'];bvid=params.get('bvid')
    v=video.Video(bvid=bvid,credential=credential) if bvid else None
    if kind=='like_video':return bool(await v.has_liked())
    if kind=='coin':return int(await v.get_pay_coins())>=params.get('number',1)
    if kind in ('follow','unfollow'):
        relation=await user.User(int(params['uid']),credential=credential).get_relation()
        return relation_matches(relation,kind=='follow')
    if kind=='favorite':
        rows=(await favorite_list.get_video_favorite_list(int(credential.dedeuserid),video=v,credential=credential)).get('list') or []
        folder=str(record.get('folder_id') or params.get('folder_id') or '')
        return any((str(r['id'])==folder if folder else r.get('title')=='默认收藏夹') and r.get('fav_state') for r in rows)
    return None


async def action(credential, action, bvid='', uid='', rpid='', root='', text='', number=1, folder_id='', task_id=None, token=None, meme_id='', path='',resource_key=''):
    """Only non-monetary Bili actions, each reserved before network writes."""
    from bilibili_api import video, user, comment, favorite_list
    if resource_key:
        from owner_social import act
        return await act(credential,action,resource_key,rpid,root,text,meme_id,path)
    if action == 'send_image':
        from owner_images import send_image
        return await send_image(credential,path=path,meme_id=meme_id,task_id=task_id,token=token)
    if action not in ('like_video', 'like_comment', 'follow', 'unfollow', 'favorite', 'coin', 'comment'):
        return {'success': False, 'error': '未知操作；涉及金钱必须先用确认流程'}
    with c.locked_state() as state:
        task, incoming = authorize(state, task_id, token)
        active_task_id = task['id']
        image_request = comment_image_requirement(state, task, incoming)
        delivery = image_delivery_requirement(state, task, incoming)
        if action=='comment' and delivery['required'] and delivery['channel']=='private':
            return {'success':False,'wrong_destination':True,'error':'这条请求是把图片私信给主人，不能改发公开评论；请用send_image'}
        if action == 'comment' and image_request['required'] and not (meme_id or path):
            return {'success': False, 'image_required': True, 'error': '主人要求真实表情图，请从bili_meme_library选meme_id，传给本工具；[图名]只是文字，尚未发送'}
        if action == 'comment' and image_request['image_only'] and len(text.strip()) > 12:
            return {'success': False, 'image_required': True, 'error': '本条只需表情图配至多12字的简短文字/原生表情；也可text留空由已确认策略补“分享图片”。不要增加长篇正文，尚未发送'}
        if action == 'comment' and (wants_video(incoming) or task.get('media_failures', {}).get(bvid)) and not video_evidence(task,bvid):
            return {'success': False, 'error': '尚未取得这个视频的实际画面与音轨分析，不能用字幕替代观看后发表观感；先完成视频理解，否则如实报告未完成'}
    if action == 'comment' and c.contains_outbound_private_info(text):
        return {'success': False, 'rewrite_required': True, 'error': '评论不能含内部控制词，请重写'}
    if c.budget_stop_reason():
        return {'success': False, 'budget_stopped': True}
    if action not in ('follow','unfollow') and not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
        return {'success': False, 'needs_input': True, 'error': '请主人明确视频链接/BV号，不能猜最近观看对象'}
    if action in ('follow','unfollow') and (not str(uid).isdigit() or int(uid) <= 0):
        return {'success': False, 'needs_input': True, 'error': '关注须有明确UP主UID；先搜索核对，重名请主人选择'}
    if action == 'like_comment' and (not str(rpid).isdigit() or int(rpid) <= 0):
        return {'success': False, 'needs_input': True, 'error': '点赞评论须明确rpid与所属视频，先读取评论核对'}
    if action == 'coin' and (not isinstance(number,int) or isinstance(number,bool) or number not in (1, 2)):
        return {'success': False, 'error': '每次投币数量须为1或2，不能自动充值或使用现金'}
    if action=='coin' and not credential.has_buvid3():
        return {'success':False,'needs_login':True,'error':'缺少已有设备登录信息，请重新登录后再投币；尚未提交'}
    if action == 'comment' and ((not text.strip() and not (meme_id or path)) or len(text) > 150):
        return {'success': False, 'error': '文字评论须1–150字；纯图可text留空并传meme_id'}
    if action == 'comment' and rpid and (not str(rpid).isdigit() or not str(root).isdigit()):
        return {'success': False, 'error': '楼内回复须有实际读取到的rpid与root'}
    params = dict(action=action, bvid=bvid, uid=str(uid), rpid=str(rpid), root=str(root), text=text, number=number, folder_id=str(folder_id))
    picture = None
    if path:
        if action!='comment' or meme_id:
            return {'success':False,'error':'评论图片请选择已核验文件path或图库meme_id之一'}
        from owner_images import artifact_picture
        with c.locked_state() as state:
            task,incoming=authorize(state,task_id,token)
            picture,proof=artifact_picture(task,path,image_delivery_requirement(state,task,incoming))
        params.update(image_sha256=proof['sha256'],path=proof['path'])
    if meme_id:
        if action != 'comment':
            return {'success': False, 'error': 'meme_id只用于评论动作'}
        if delivery.get('capture_required'):
            return {'success':False,'image_required':True,'error':'这条目标要求真实截图，请用浏览器截图并传已核验path，不用图库图片或重绘图替代'}
        try:
            from memes import load_picture
            picture, record = load_picture(meme_id)
        except Exception:
            return {'success': False, 'error': '没有这张已保存表情图，尚未发送；请重新选择真实meme_id'}
        if record.get('private_only') or record.get('audience') == 'owner':
            return {'success': False, 'error': '这张图只适合私下给主人，不能用于公开评论'}
        params['meme_id'] = meme_id
    key = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    if action=='coin':
        # A previous process may have died after submission. Reconcile its
        # recorded desired total read-only before deciding whether to block.
        with c.locked_state() as state:
            authorize(state,task_id,token)
            pending_coins=[(t['id'],k,dict(a)) for t in jobs(state).values() for k,a in t.get('actions',{}).items()
                if a.get('params',{}).get('action')=='coin' and a['params'].get('bvid')==bvid and a.get('status') in ('sending','uncertain')]
        for previous_id,previous_key,previous in pending_coins:
            try:verified=await reconcile_state(credential,previous)
            except Exception:verified=False
            if verified:
                with c.locked_state() as state:
                    authorize(state,task_id,token)
                    old=jobs(state)[previous_id]['actions'][previous_key]
                    if old.get('status') in ('sending','uncertain'):
                        old.setdefault('verification_history',[]).append({'previous_status':old['status'],'at':time.time()})
                        old.update(status='success',success=True,uncertain=False,readback_reconciled=True,verified_at=time.time())
    with c.locked_state() as state:
        task, _ = authorize(state, task_id, token)
        existing = task['actions'].get(key)
        if not existing and action=='coin' and any(a.get('params',{}).get('action')=='coin' and
                a['params'].get('bvid')==bvid and a.get('status') in ('sending','uncertain')
                for previous in jobs(state).values() for a in previous.get('actions',{}).values()):
            return {'success':False,'uncertain':True,'error':'这个视频已有投币提交待核验，先查既有回执，不能跨任务重投'}
        if not existing and any(r.get('status') in ('sending', 'uncertain') for r in task['actions'].values()):
            return {'success': False, 'error': '已有操作回执不明，先核查，不能继续重复提交'}
        if not existing:task['actions'][key] = {'status': 'sending', 'params': params, 'at': time.time()}
    if existing:
        if existing.get('status')!='success' and action in ('like_video','favorite','coin','follow','unfollow'):
            try:verified=await reconcile_state(credential,existing)
            except Exception:verified=False
            if verified:
                with c.locked_state() as state:
                    task,_=authorize(state,task_id,token)
                    current=task['actions'][key]
                    current.setdefault('verification_history',[]).append({'previous_status':current['status'],'at':time.time()})
                    current.update(status='success',success=True,uncertain=False,verified_at=time.time(),readback_reconciled=True)
                    existing=dict(current)
        return {'duplicate':True,'read_only':True,**existing}
    v = video.Video(bvid=bvid, credential=credential) if bvid else None
    submitted = False
    try:
        if action == 'like_video':
            before = bool(await v.has_liked())
            if not before:
                submitted = True
                await v.like(True)
            result = {'success': bool(await confirm_state(v.has_liked,bool)), 'already_done': before, 'bvid': bvid}
        elif action in ('follow','unfollow'):
            u = user.User(int(uid), credential=credential)
            before = (await u.get_relation()).get('relation', {}).get('attribute', 0) in (2, 6)
            desired=action=='follow'
            if before != desired:
                submitted = True
                await u.modify_relation(user.RelationType.SUBSCRIBE if desired else user.RelationType.UNSUBSCRIBE)
            relation=await confirm_state(u.get_relation,lambda r:relation_matches(r,desired))
            after=relation.get('relation', {}).get('attribute', 0) in (2, 6)
            result = {'success': relation_matches(relation,desired), 'already_done': before==desired, 'following':after, 'uid': str(uid)}
        elif action == 'favorite':
            lists = await favorite_list.get_video_favorite_list(int(credential.dedeuserid), video=v, credential=credential)
            rows = lists.get('list') or []
            chosen = next((r for r in rows if str(r['id']) == str(folder_id)), None) if folder_id else next((r for r in rows if r.get('title') == '默认收藏夹'), rows[0] if len(rows) == 1 else None)
            if not chosen:
                result = {'success': False, 'needs_input': True, 'error': '请明确已有收藏夹', 'folders': [{'id': str(r['id']), 'title': r.get('title')} for r in rows]}
            else:
                before = bool(chosen.get('fav_state'))
                if not before:
                    submitted = True
                    await v.set_favorite(add_media_ids=[int(chosen['id'])])
                after = await confirm_state(lambda:favorite_list.get_video_favorite_list(int(credential.dedeuserid), video=v, credential=credential),
                    lambda data:any(str(r['id'])==str(chosen['id']) and r.get('fav_state') for r in data.get('list') or []))
                result = {'success': any(str(r['id']) == str(chosen['id']) and r.get('fav_state') for r in after.get('list') or []), 'already_done': before, 'folder_id': str(chosen['id']), 'bvid': bvid}
        elif action == 'coin':
            before = int(await v.get_pay_coins())
            # number is the desired total on this video, not an extra repeated payment.
            if before < number:
                submitted = True
                def authorize_coin():
                    with c.locked_state() as state:authorize(state,task_id,token)
                    if c.budget_stop_reason():raise PermissionError('费用保护')
                receipt=await submit_coin(credential,bvid,number-before,expected_before=before,authorize_write=authorize_coin)
            total=int(await confirm_state(v.get_pay_coins,lambda value:int(value)>=number))
            result = {'success': total >= number, 'already_done': before >= number, 'coins_total':total, 'bvid': bvid}
            if submitted:result['coin_receipt']=receipt
        elif action == 'like_comment':
            info = await v.get_info()
            submitted = True
            response = await comment.Comment(int(info['aid']), comment.CommentResourceType.VIDEO, int(rpid), credential).like(True)
            result = {'success': True, 'accepted': True, 'verified_visible': False, 'rpid': str(rpid), 'receipt': response}
        else:
            info = await v.get_info()
            with c.locked_state() as state:
                scheduled = max(time.time(), state.get('last_scheduled_at', 0) + c.MIN_SEND_INTERVAL, state.get('last_sent_at', 0) + c.MIN_SEND_INTERVAL)
                state['last_scheduled_at'] = scheduled
            await asyncio.sleep(max(0, scheduled - time.time()))
            with c.locked_state() as state:
                authorize(state, task_id, token)
            if c.budget_stop_reason():
                raise PermissionError('费用保护触发，取消提交')
            from reply_publish import send_comment_native
            submitted = True
            response = await send_comment_native(text, int(info['aid']), comment.CommentResourceType.VIDEO, credential, root=int(root) if rpid else None, parent=int(rpid) if rpid else None, **({'pic': picture} if picture else {}))
            sent = str(response.get('rpid_str') or response.get('rpid') or (response.get('reply') or {}).get('rpid') or '')
            if not sent.isdigit():
                raise RuntimeError('评论回执没有ID，不能重发')
            with c.locked_state() as state:
                target_root = str(root) if rpid else sent
                state['roots'].setdefault(target_root, {'bvid': bvid, 'aid': int(info['aid']), 'created_at': int(time.time()), 'owned': not bool(rpid)})
                state['sent'][sent] = target_root
                if not rpid:
                    state.setdefault('video_attempts', {})[bvid] = sent
                    counts = c.day_counts(state)
                    if bvid not in counts['videos']:
                        counts['videos'].append(bvid)
                state.setdefault('native_dynamic_requests', {})[sent] = {'requested': True, 'at': int(time.time()), 'server_has_sync_dynamic': response.get('has_sync_dynamic')}
                state['recent_public'] = (state.get('recent_public', []) + [{'rpid': sent, 'bvid': bvid, 'text': text}])[-20:]
                c.record_reply_ending(state, text)
                state['last_sent_at'] = time.time()
                if meme_id and response.get('_image_receipt', {}).get('image_attached'):
                    state['memes'][meme_id]['used'] = state['memes'][meme_id].get('used', 0) + 1
            image_receipt = response.get('_image_receipt') or {}
            attached = bool(picture and image_receipt.get('image_attached'))
            result = {'success': not picture or attached, 'rpid': sent, 'bvid': bvid,
                      'dynamic_forward_requested': True, 'verified_visible': bool(image_receipt.get('verified_visible')),
                      'image_attached': attached, 'meme_id': meme_id or None}
            if picture:
                result['image_receipt'] = image_receipt
                if not attached:
                    result.update(accepted=True, uncertain=True, message='评论已有ID，但未核验图片附上；先查原评论，不能重发或改发文字')
        if submitted and not result.get('success'):
            result.update(uncertain=True,accepted=True,message='平台已接受提交，读回状态尚未一致；再次调用相同动作只读核查，不会重发，也不能改走网页重复提交')
        status = 'success' if result.get('success') else ('uncertain' if result.get('uncertain') else 'failed')
    except c.ReplyPaused as error:
        submitted=False
        status,result='failed',{'success':False,'submitted':False,'reply_paused':True,'uncertain':False,'error':str(error)}
    except CoinNotSubmitted as error:
        submitted=False
        status,result='failed',{'success':False,'submitted':False,'error':str(error),'uncertain':False,'diagnostic':getattr(error,'diagnostic',None)}
    except ResponseCodeException as error:
        status, result = 'failed', platform_error(error,credential)
    except Exception as error:
        status = 'uncertain' if submitted else 'failed'
        result = {'success': False, 'error': type(error).__name__, 'uncertain': submitted, 'message': '回执不明，不自动重发' if submitted else '提交前失败，没有执行写操作'}
    with c.locked_state() as state:
        task = jobs(state)[active_task_id]
        current=task['actions'][key]
        if current.get('status')=='success' and current.get('readback_reconciled') and not result.get('success'):
            current.setdefault('verification_history',[]).append({'local_result':status,'at':time.time()})
            return {'read_only':True,**current}
        task['actions'][key].update(status=status, **result, write_attempted=submitted,finished_at=time.time())
    return {'status': status, **result}
