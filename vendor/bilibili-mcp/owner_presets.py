"""Conservative code routes for simple goals; everything else stays with the agent."""
import json
import re


def parse(item):
    from owner_tasks import command_text
    text = command_text(item).strip()
    text = re.sub(r'^(?:(?:小肥鱼|大肥鱼|你|请你|请|麻烦你?|帮我|给我|可以|能不能|能否|去|先|顺便|再)[，,\s]*)+', '', text)
    raw_text = text
    text = text.rstrip('。！!')
    search_goal = re.fullmatch(r'(?:搜索视频|搜一下视频|搜视频)[：:\s]*(.{1,80})', text)
    if search_goal:
        return {'kind': 'search_video', 'keyword': search_goal.group(1)}
    follow = re.fullmatch(r'关注\s*(?:UP主|用户|账号)?\s*(?:UID\s*[:：]?\s*)?(\d+)', text, re.I)
    profile = re.fullmatch(r'关注\s*https?://space\.bilibili\.com/(\d+)/?(?:\?\S*)?', text)
    if follow or profile:
        return {'kind': 'follow', 'uid': (follow or profile).group(1)}
    named = re.fullmatch(r'关注(.{1,50}?)(?:的)?官方账号[，,]\s*账号名字就叫(.{1,50})', text)
    official = re.fullmatch(r'关注(.{1,50}?)(?:的)?官方账号', text)
    ordinary = re.fullmatch(r'关注(.{1,50}?)(?:这个账号|账号|UP主)?', text)
    if named or official or ordinary:
        raw_name = (named.group(2) if named else (official or ordinary).group(1)).strip()
        quoted = bool(re.fullmatch(r'[“"「].+[”"」]', raw_name))
        # Demonstratives refer to conversation/images, not a literal account.
        if not quoted and (re.match(r'^(?:这|那|他们|她们|它们|第|上一个|前一个|后一个|上面|前面|刚才|之前|上述|前述)', raw_name)
                or re.match(r'^[一二三四五六七八九十两俩几0-9]+(?:个|位|名)(?:人|账号|用户|博主|UP|up)',raw_name)
                or re.fullmatch(r'他|她|它|对方|前者|后者|楼主|UP主|up主', raw_name)
                or re.search(r'图片|截图|图里|图中|上面|前面|刚才', raw_name)):
            return None
        name = (named.group(2) if named else (official or ordinary).group(1)).strip(' “”"「」')
        if name and not re.search(r'然后|并|再|取消|不要|不用|别|最新|视频|评论|收藏|，|,|[。！？!?：:\s/<>]', name):
            return {'kind': 'follow', 'name': name, 'official': bool(named or official)}
    bvids = set(re.findall(r'BV[0-9A-Za-z]{10}', text))
    if len(bvids) > 1:
        return None
    bvid = next(iter(bvids), None)
    comment_goal = re.fullmatch(r'(?:给|在)?(BV[0-9A-Za-z]{10})(?:视频)?(?:发一条评论|评论|回复)[：:]\s*(.{1,150})', raw_text, re.S)
    if comment_goal:
        return {'kind': 'comment', 'bvid': comment_goal.group(1), 'text': comment_goal.group(2)}
    normalized = re.sub(r'https?://(?:www\.)?bilibili\.com/video/BV[0-9A-Za-z]{10}/?(?:\?[^\s，,。！!]*)?', '<video>', text)
    normalized = re.sub(r'BV[0-9A-Za-z]{10}', '<video>', normalized)
    if not bvid and item.get('bvid') and re.search(r'这个视频|当前视频|该视频', normalized):
        bvid = item['bvid']
        normalized = re.sub(r'这个视频|当前视频|该视频', '<video>', normalized)
    if not bvid:
        return None
    normalized = normalized.replace('：', '').replace(':', '').replace(' ', '')
    normalized = normalized.removeprefix('把')
    for verb, actions in [(r'点赞|点个赞|点一下赞|赞一下', [('like_video', 1)]),
                          (r'投币|投1个币|投一枚币|投一个币|投1币', [('coin', 1)]),
                          (r'投2个币|投两枚币|投两个币|投两币|投2币', [('coin', 2)]),
                          (r'收藏(?:到默认收藏夹)?', [('favorite', 1)]),
                          (r'三连|一键三连', [('like_video', 1), ('coin', 1), ('favorite', 1)])]:
        if re.fullmatch(r'(?:给)?<video>(?:' + verb + r')|(?:' + verb + r')<video>(?:到默认收藏夹)?', normalized):
            return {'kind': 'video_actions', 'bvid': bvid, 'actions': actions}
    return None


async def run(credential, task_id, token):
    import conversation as c
    import owner_tasks as tasks
    from bilibili_api import search, user
    with c.locked_state() as state:
        task, incoming = tasks.authorize(state, task_id, token)
        preset = parse(incoming)
        if task.get('route') != 'preset' or not preset:
            raise PermissionError('这不是可由代码直接处理的简单任务')
    tasks.plan(['代码核对真实目标', '执行原消息明确要求的动作，复用已有回执', '读取结果并回复'], task_id, token)
    try:
        if preset['kind'] == 'search_video':
            response = await search.search_by_type(keyword=preset['keyword'], search_type=search.SearchObjectType.VIDEO, page=1)
            videos = (response.get('result') or [])[:3]
            tasks.record_tool('bili_search', task_id=task_id, token=token)
            if not videos:
                return tasks.propose('completed', '主人，这次搜索没有返回视频候选。', task_id=task_id, token=token)
            rows = [re.sub(r'<[^>]+>', '', str(r.get('title') or ''))[:22] + ' ' + str(r.get('bvid') or '') for r in videos]
            return tasks.propose('completed', ('主人，搜索到：\n' + '\n'.join(rows))[:150], task_id=task_id, token=token)
        elif preset['kind'] == 'comment':
            result = await tasks.action(credential, 'comment', bvid=preset['bvid'], text=preset['text'], task_id=task_id, token=token)
            success_text = '主人，指定评论已提交，已请求随评论转到动态；公开可见性仍需复核。'
        elif preset['kind'] == 'follow':
            uid, name = preset.get('uid'), preset.get('name')
            if not uid:
                response = await search.search_by_type(keyword=name, search_type=search.SearchObjectType.USER, page=1, page_size=20)
                candidates = [r for r in response.get('result') or [] if r.get('uname') == name
                              and (not preset.get('official') or (r.get('official_verify') or {}).get('type') == 1)]
                if len(candidates) != 1:
                    return tasks.propose('needs_input', f'主人，没能唯一确认“{name}”这个账号，请给我它的主页链接或UID。', task_id=task_id, token=token)
                uid = str(candidates[0]['mid'])
            info = await user.User(int(uid), credential=credential).get_user_info()
            if ((name and info.get('name') != name) or (preset.get('official') and
                    ((info.get('official') or {}).get('type') != 1 or not (info.get('official') or {}).get('title')))):
                return tasks.propose('needs_input', '主人，这个账号的昵称或官方认证没有核对上，请给我准确的主页链接。', task_id=task_id, token=token)
            result = await tasks.action(credential, 'follow', uid=uid, task_id=task_id, token=token)
            success_text = f'主人，已确认关注“{str(info["name"])[:40]}”（UID {uid}）。'
        else:
            for action, number in preset['actions']:
                result = await tasks.action(credential, action, bvid=preset['bvid'], number=number, task_id=task_id, token=token)
                if not result.get('success'):
                    break
            labels = {'like_video': '点赞', 'coin': '投币', 'favorite': '收藏'}
            success_text = '主人，已核验完成' + '、'.join(labels[a] for a, _ in preset['actions']) + '：' + preset['bvid'] + '。'
        if result.get('success'):
            return tasks.propose('completed', success_text, task_id=task_id, token=token)
        status = 'needs_input' if result.get('needs_input') else 'failed'
        return tasks.propose(status, '主人，这次操作未确认完成。' + ('已有回执不明，先核查，不能重复提交。' if result.get('uncertain') else '请核对目标和账号状态，已成功的步骤不会重复执行。'), task_id=task_id, token=token)
    except Exception as error:
        with c.locked_state() as state:
            task, _ = tasks.authorize(state, task_id, token)
            task['preset_error'] = type(error).__name__
        return tasks.propose('failed', '主人，这次代码执行未能取得完整结果，已有操作记录已保留，暂不重复提交。', task_id=task_id, token=token)
