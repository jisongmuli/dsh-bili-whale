"""Authenticated owner image delivery; private originals and persistent receipts."""
import asyncio
import hashlib
import io
import json
import os
import time
from pathlib import Path

from PIL import Image
from bilibili_api import session
from bilibili_api.exceptions import ResponseCodeException
import conversation as c


def artifact_picture(task,path,intent):
    """Bind an immutable image and, when requested, a real capture to this owner task."""
    if task.get('scope')!='full':raise PermissionError('文件附件需要有效的通用主人任务')
    target=Path(path).resolve()
    if not Path(path).is_absolute() or not target.is_file():
        raise PermissionError('附件必须是已核验的普通产物图片')
    data=target.read_bytes();digest=hashlib.sha256(data).hexdigest()
    proof=next((a for a in task.get('artifacts',[]) if a.get('path')==str(target) and a.get('sha256')==digest and a.get('bytes')==len(data)),None)
    if not proof:raise PermissionError('文件尚未登记或内容已改变，不能上传')
    managed_capture=(target.is_relative_to((c.HOME/'owner-captures').resolve()) and proof.get('capture',{}).get('kind')=='browser_screenshot')
    if any(p.lower() in ('.dsh','.gemini','.ssh','.codex') for p in target.parts) and not managed_capture:
        raise PermissionError('运行数据和凭证不能作为图片附件')
    if intent.get('capture_required') and (proof.get('capture',{}).get('kind')!='browser_screenshot' or not proof['capture'].get('source_url')):
        raise PermissionError('任务要求真实截图，普通生成图片不能充当页面截图；先用网页截图工具保存真实画面')
    if len(data)>5*1024*1024:raise ValueError('附件超过5MB，尚未上传')
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ('PNG','JPEG','WEBP','GIF'):raise ValueError('附件不是支持的真实图片')
        image.verify()
    from memes import OriginalMemePicture
    return OriginalMemePicture(target),proof


async def send_image(credential, path='', meme_id='', task_id=None, token=None):
    import owner_tasks as o
    with c.locked_state() as state:
        task, incoming = o.authorize(state,task_id,token)
        active_id=task['id']
        intent=o.image_delivery_requirement(state,task,incoming)
        if not intent['required'] or intent['channel']!='private':
            return {'success':False,'error':'本条真实命令没有要求私信发图，不能擅自发送'}
        if not c.BOT_UID or str(credential.dedeuserid)!=c.BOT_UID:
            raise PermissionError('当前账号不是预期发送账号')
        if bool(path)==bool(meme_id):
            return {'success':False,'error':'请选择一个真实本地图片path或已有meme_id，二者不能同时填'}
        if path and task.get('scope')!='full':
            raise PermissionError('本地文件需对应真实的电脑文件任务')
        artifacts=list(task.get('artifacts',[]))
    try:
        if path:
            if intent.get('capture_required'):artifact_picture(task,path,intent)
            target=Path(path)
            if not target.is_absolute() or not target.is_file():
                raise ValueError('请先找到实际存在的绝对路径图片')
            target=target.resolve()
            if any(part.lower() in ('.dsh','.gemini','.ssh','.codex') for part in target.parts):
                artifact_picture(task,path,intent)  # only task-bound managed captures can pass
            if target.stat().st_size>5*1024*1024:
                raise ValueError('图片超过5MB，尚未上传；不能擅自丢弃动图帧')
            data=target.read_bytes()
            digest=hashlib.sha256(data).hexdigest()
            if not any(Path(a.get('path','')).resolve()==target and a.get('sha256')==digest and a.get('bytes')==len(data) for a in artifacts):
                raise ValueError('请先用bili_owner_artifact核验所选图片；找到文件不代表已经发送')
            filename=target.name
        else:
            from memes import load_picture
            picture, record=load_picture(meme_id)
            data=picture.original_path.read_bytes()
            digest=hashlib.sha256(data).hexdigest()
            filename=picture.original_path.name
        with Image.open(io.BytesIO(data)) as image:
            format_=str(image.format).lower()
            frames=getattr(image,'n_frames',1)
            width,height=image.size
            image.verify()
        if format_ not in ('png','jpeg','webp','gif'):
            raise ValueError('仅支持PNG、JPEG、WebP和GIF原图')
    except (ValueError,OSError) as error:
        return {'success':False,'error':str(error)[:180],'submitted':False}
    params={'action':'send_image','recipient_uid':c.OWNER_UID,'sha256':digest,'destination':'private'}
    key=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:24]
    with c.locked_state() as state:
        task,_=o.authorize(state,task_id,token)
        earlier=next((a for a in task['actions'].values() if a.get('params',{}).get('action')=='send_image' and a.get('status') in ('preparing','sending','success','uncertain')),None)
        if earlier:
            return {'duplicate':True,**earlier}
        existing=task['actions'].get(key)
        if existing:
            return {'duplicate':True,**existing}
        if c.budget_stop_reason():
            return {'success':False,'budget_stopped':True}
        scheduled=max(time.time(),state.get('last_scheduled_at',0)+c.MIN_SEND_INTERVAL,state.get('last_sent_at',0)+c.MIN_SEND_INTERVAL)
        state['last_scheduled_at']=scheduled
        task['actions'][key]={'status':'preparing','params':params,'at':time.time(),'filename':filename,
                              'bytes':len(data),'frames':frames,'width':width,'height':height}
    # Store a private immutable copy, so later file edits cannot change the upload.
    root=(c.STATE_FILE.parent/'owner-attachments').resolve()
    root.mkdir(exist_ok=True)
    stored=root/(digest+'.'+('jpg' if format_=='jpeg' else format_))
    try:
        if not stored.exists():
            temp=stored.with_suffix('.'+str(os.getpid())+'.tmp')
            temp.write_bytes(data);os.replace(temp,stored)
        if hashlib.sha256(stored.read_bytes()).hexdigest()!=digest:
            raise ValueError('私有图片副本校验失败')
        from memes import OriginalMemePicture
        picture=OriginalMemePicture(stored)
        await asyncio.sleep(max(0,scheduled-time.time()))
        with c.locked_state() as state:
            task,_=o.authorize(state,task_id,token)
            if c.budget_stop_reason():
                task['actions'][key].update(status='failed',success=False,error='费用保护触发，提交前停止')
                return task['actions'][key]
            task['actions'][key]['status']='sending'
        try:
            result=await session.send_msg(credential=credential,receiver_id=int(c.OWNER_UID),
                msg_type=session.EventType.PICTURE,content=picture)
            receipt=str((result or {}).get('msg_key') or '')
            outcome={'status':'success' if receipt.isdigit() else 'uncertain','success':receipt.isdigit(),
                     'image_sent':receipt.isdigit(),'reply_msg_key':receipt,'recipient_uid':c.OWNER_UID,
                     'sha256':digest,'frames':frames,'image_url':picture.url,'forwards_to_dynamic':False}
        except ResponseCodeException as error:
            outcome={'status':'failed','success':False,'image_sent':False,'code':error.code,'error':'B站拒绝图片私信，不自动重试'}
        except Exception:
            outcome={'status':'uncertain','success':False,'image_sent':False,'uncertain':True,'error':'图片私信回执不明，不重发'}
    except Exception as error:
        outcome={'status':'failed','success':False,'image_sent':False,'error':type(error).__name__,'submitted':False}
    with c.locked_state() as state:
        task=state['owner_tasks'][active_id]
        task['actions'][key].update(outcome,finished_at=time.time())
        if outcome.get('success'):
            import private_messages as dm
            private=dm.section(state)
            private['last_owner_activity']=state['last_sent_at']=time.time()
            private['history'][c.OWNER_UID]=(private['history'].get(c.OWNER_UID,[])+[{
                'role':'outgoing','in_reply_to':task['source_id'],'msg_key':outcome['reply_msg_key'],
                'text':'[发送图片：'+filename+']','message_type':2,'image_sha256':digest,'time':time.time()}])[-12:]
        return {**outcome,'message':'图片已通过私信发送，未发评论或动态' if outcome.get('success') else outcome.get('error')}
