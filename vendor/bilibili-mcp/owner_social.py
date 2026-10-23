"""Read and reply to verified Bilibili resources without writing SDK scripts."""
import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from bilibili_api import comment, opus, video
from bilibili_api.exceptions import ResponseCodeException
import conversation as c
import owner_tasks as tasks


def normalize(row):
    return {'rpid':str(row.get('rpid_str') or row.get('rpid') or ''),
        'root':str(row.get('root_str') or row.get('root') or ''),
        'parent':str(row.get('parent_str') or row.get('parent') or ''),
        'uid':str((row.get('member') or {}).get('mid') or row.get('mid') or ''),
        'user':(row.get('member') or {}).get('uname',''),
        'content':(row.get('content') or {}).get('message',''),
        'pictures':(row.get('content') or {}).get('pictures') or [],'ctime':row.get('ctime')}


async def resource(credential,target):
    target=str(target).strip()
    if 'b23.tv/' in target:
        result=await tasks.resolve_video(target)
        target=result.get('bvid') or result.get('url') or target
    match=re.search(r'BV[0-9A-Za-z]{10}',target)
    if match:
        v=video.Video(bvid=match[0],credential=credential);data=await v.get_info()
        return {'oid':str(data['aid']),'type':1,'kind':'video','bvid':match[0],
            'url':'https://www.bilibili.com/video/'+match[0],'title':data.get('title',''),'author_uid':str((data.get('owner') or {}).get('mid') or '')}
    parsed=urlsplit(target)
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password or parsed.port not in (None,80,443):
        raise ValueError('请提供真实B站视频或动态链接')
    if parsed.hostname not in ('www.bilibili.com','bilibili.com','t.bilibili.com','m.bilibili.com'):
        raise ValueError('只读取B站资源，不能把其他网页当作评论对象')
    match=re.fullmatch(r'/(?:opus/|dynamic/)?(\d+)/?',parsed.path)
    if not match:raise ValueError('无法识别评论资源，请用真实视频BV或动态/opus链接')
    data=(await opus.Opus(int(match[1]),credential=credential).get_info())['item']
    basic=data['basic'];kind=int(basic['comment_type'])
    if kind not in (11,12,17):raise ValueError('该资源评论类型暂不能通过直接工具操作，请用通用网页工具')
    return {'oid':str(basic['comment_id_str']),'type':kind,'kind':'dynamic',
        'url':'https://www.bilibili.com/opus/'+match[1],'title':basic.get('title',''),'author_uid':str(basic.get('uid') or '')}


async def fetch(credential,res,root='',limit=30):
    type_=comment.CommentResourceType(res['type'])
    if root:
        if not str(root).isdigit():raise ValueError('root必须是读到的真实楼层ID')
        data=await comment.Comment(int(res['oid']),type_,int(root),credential=credential).get_sub_comments(page_size=30)
        rows=([data['root']] if data.get('root') else [])+(data.get('replies') or [])
    else:
        data=await comment.get_comments_lazy(int(res['oid']),type_,credential=credential)
        rows=data.get('replies') or []
        top=data.get('top_replies') or []
        rows=top+rows
    output={}
    for row in rows[:max(1,min(60,int(limit)))]:
        for entry in [row]+list(row.get('replies') or []):
            item=normalize(entry)
            if item['rpid'].isdigit():output[item['rpid']]=item
    return list(output.values())


async def read(credential,target,root='',limit=30):
    with c.locked_state() as state:task,_=tasks.authorize(state);task_id=task['id']
    res=await resource(credential,target)
    rows=await fetch(credential,res,root,limit)
    key=hashlib.sha256((str(res['type'])+':'+res['oid']).encode()).hexdigest()[:20]
    with c.locked_state() as state:
        task,_=tasks.authorize(state)
        if task['id']!=task_id:raise PermissionError('任务已变化')
        prior=task.setdefault('resources',{}).get(key,{})
        seen=prior.get('seen_comments',{})
        seen.update({r['rpid']:r for r in rows})
        from reply_controls import remember_owner
        for row in rows:
            remember_owner(state, {**row,'oid':res['oid'],'comment_type':res['type']})
        task['resources'][key]={**res,'seen_comments':dict(list(seen.items())[-200:]),'read_at':time.time()}
        if root:task['resources'][key]['last_root']=str(root)
        task['progress']={'stage':'已读取目标和楼层，正在组织回复','at':time.time()}
    return {'resource_key':key,'resource':res,'comments':rows,
        'next':'回复直接用bili_owner_action(action="comment",resource_key=本次key,rpid=目标rpid,text=正文)，root由实际楼层自动确定。无需写脚本或读取程序源码。'}


async def act(credential,action,resource_key,rpid='',root='',text='',meme_id='',path=''):
    if action not in ('comment','like_comment'):return {'success':False,'error':'该资源工具只处理评论与点赞评论'}
    with c.locked_state() as state:
        task,incoming=tasks.authorize(state)
        task_id,token=task['id'],task['token']
        from reply_publish import comment_backoff
        if action=='comment' and (cooling:=comment_backoff(state)):return cooling
        res=task.get('resources',{}).get(resource_key)
        if not res:return {'success':False,'error':'先用bili_owner_comments实际读取这个目标，不能猜资源编号'}
        parent=res['seen_comments'].get(str(rpid)) if rpid else None
        if rpid and not parent:return {'success':False,'error':'回复目标尚未实际读取，先读取所属完整楼层'}
        actual_root=(parent['root'] if parent['root'] not in ('','0') else parent['rpid']) if parent else ''
        if root and str(root)!=actual_root:return {'success':False,'error':'root与实际楼层不符，尚未提交'}
        if action=='like_comment' and not parent:return {'success':False,'needs_input':True,'error':'请明确要点赞的真实评论'}
        requirement=tasks.image_delivery_requirement(state,task,incoming)
        if action=='comment' and requirement['required'] and (requirement['channel']!='comment' or not (meme_id or path)):
            return {'success':False,'error':'本任务要求真实图片，请核对目的地并选择真实meme_id，不能改发纯文字'}
        if action=='comment' and requirement.get('image_only') and len(text.strip())>12:
            return {'success':False,'error':'只发表情图时配至多12字简短文字，不增加长篇正文'}
        if action=='comment' and requirement['required'] and re.search(r'几张|多张|[两二三23]张',tasks.command_text(incoming)):
            return {'success':False,'error':'本次需要多张图片，请用通用网页工具一次附足图片，不拆成多条文字或单图评论'}
        if res.get('kind')=='video' and tasks.wants_video(incoming) and not tasks.video_evidence(task,res.get('bvid')):
            return {'success':False,'error':'观看后回复须先取得本视频的真实音画理解证据'}
    if action=='comment' and (c.contains_outbound_private_info(text) or len(text)>150 or (not text.strip() and not (meme_id or path))):
        return {'success':False,'error':'请提供150字以内的自然回复；图片可配简短文字，不含内部信息'}
    picture=None
    proof=None
    if path:
        if action!='comment' or meme_id:raise ValueError('选择文件path或图库meme_id之一')
        from owner_images import artifact_picture
        picture,proof=artifact_picture(task,path,requirement)
    if meme_id:
        if requirement.get('capture_required'):raise PermissionError('本任务需要真实截图，不能用图库图片替代')
        from memes import load_picture
        picture,record=load_picture(meme_id)
        if record.get('private_only') or record.get('audience')=='owner':raise PermissionError('私人图片不能公开')
    params={'action':action,'resource_key':resource_key,'oid':res['oid'],'type':res['type'],
        'rpid':str(rpid),'root':actual_root,'text':text,'meme_id':meme_id}
    if proof:params.update(image_sha256=proof['sha256'],path=proof['path'])
    # One logical reply to this parent per task; changing wording must not
    # turn an accepted or uncertain send into a new submission.
    identity={k:v for k,v in params.items() if k not in ('text','meme_id')}
    key=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]
    with c.locked_state() as state:
        task,_=tasks.authorize(state,task_id,token)
        if key in task['actions']:return {'duplicate':True,**task['actions'][key]}
        if any(a['status'] in ('sending','uncertain') for a in task['actions'].values()):
            return {'success':False,'uncertain':True,'error':'已有回执不明的操作，不能重复发送'}
        task['actions'][key]={'status':'sending','params':params,'at':time.time()}
    submitted=False
    try:
        if c.budget_stop_reason():raise PermissionError('费用保护触发')
        type_=comment.CommentResourceType(res['type'])
        if action=='like_comment':
            submitted=True
            await comment.Comment(int(res['oid']),type_,int(rpid),credential=credential).like(True)
            result={'success':True,'rpid':str(rpid),'resource':res['url']}
        else:
            with c.locked_state() as state:
                due=max(time.time(),state.get('last_scheduled_at',0)+c.MIN_SEND_INTERVAL,state.get('last_sent_at',0)+c.MIN_SEND_INTERVAL)
                state['last_scheduled_at']=due
            await asyncio.sleep(max(0,due-time.time()))
            with c.locked_state() as state:tasks.authorize(state,task_id,token)
            if c.budget_stop_reason():raise PermissionError('费用保护触发')
            from reply_publish import send_comment_native
            submitted=True
            response=await send_comment_native(text,int(res['oid']),type_,credential,
                root=int(actual_root) if parent else None,parent=int(rpid) if parent else None,
                **({'pic':picture} if picture else {}))
            sent=str(response.get('rpid_str') or response.get('rpid') or (response.get('reply') or {}).get('rpid') or '')
            if not sent.isdigit():raise RuntimeError('缺少真实评论ID')
            image=response.get('_image_receipt') or {}
            result={'success':not picture or bool(image.get('image_attached')),'rpid':sent,'resource':res['url'],
                'oid':res['oid'],'type':res['type'],'parent':str(rpid),'root':actual_root or sent,
                'dynamic_forward_requested':True,'image_attached':bool(image.get('image_attached')),
                'verified_visible':bool(image.get('verified_visible'))}
            if picture and not result['image_attached']:result.update(accepted=True,uncertain=True)
            with c.locked_state() as state:
                actual_key=actual_root or sent
                state['roots'].setdefault(actual_key,{'bvid':res.get('bvid',''),'aid':res['oid'],'oid':str(res['oid']),
                    'comment_type':res['type'],'resource_url':res['url'],'resource_title':res.get('title',''),
                    'owned':not bool(parent),'created_at':int(time.time())})
                state['sent'][sent]=actual_key
                state.setdefault('native_dynamic_requests',{})[sent]={'requested':True,'at':int(time.time()),'server_has_sync_dynamic':response.get('has_sync_dynamic')}
                state['last_sent_at']=time.time();c.record_reply_ending(state,text)
        status='success' if result['success'] else 'uncertain'
    except c.ReplyPaused as error:
        submitted=False
        status='failed';result={'success':False,'submitted':False,'reply_paused':True,'uncertain':False,'error':str(error)}
    except ResponseCodeException as error:
        status='failed';result={'success':False,'error':f'B站拒绝操作（{error.code}），不自动重试'}
    except Exception as error:
        status='uncertain' if submitted else 'failed'
        result={'success':False,'uncertain':submitted,'error':type(error).__name__}
    with c.locked_state() as state:
        task=state['owner_tasks'][task_id]
        task['actions'][key].update(status=status,**result,finished_at=time.time())
        task['progress']={'stage':'已取得评论提交回执' if status=='success' else '操作未确认完成，保留记录待核查','at':time.time()}
        if status=='success':
            state['owner_focus']={'task_id':task_id,'resource':{k:v for k,v in res.items() if k!='seen_comments'},
                'root':result.get('root') or actual_root,'parent':str(rpid),'verified_at':time.time()}
    return {'status':status,**result}
