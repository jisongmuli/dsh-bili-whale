"""Narrow local state access for the official CLI; never exposes credentials."""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import conversation as c

HOME = Path(__file__).resolve().parent


def atomic(path, value):
    temp = path.with_suffix(f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def access(document='persona', mode='read', content='', source_id='', channel='dm', delta=0, private=False):
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    targets = {'persona': HOME/'persona-state.json', 'task': HOME/'duty.md', 'seen': HOME/'seen.md',
               'report': HOME/'out'/f'{now:%Y-%m-%d}.md', 'archive': HOME/'digest'/f'{now:%Y-%m}.md',
               'owner_requests': HOME/'owner-requests.md'}
    if document not in targets or mode not in ('read', 'append', 'owner_reply'):
        raise ValueError('Unknown local document or operation')
    if private and (document != 'persona' or mode == 'append'):
        raise ValueError('私信轮只可读取或更新好感度，不能写公开报告或记忆')
    path = targets[document]
    with c.locked_state() as conversation:
        if document == 'persona':
            state = json.loads(path.read_text(encoding='utf-8'))
            if mode == 'owner_reply':
                if not isinstance(delta, int) or isinstance(delta, bool) or not -3 <= delta <= 3:
                    raise ValueError('好感度每次只能变化-3到3')
                if channel == 'dm':
                    receipt = conversation.get('private_messages', {}).get('handled', {}).get(str(source_id), {})
                    valid = receipt.get('status') == 'sent' and str(receipt.get('uid')) == c.OWNER_UID
                    last_key = 'last_affection_dm_id'
                elif channel == 'comment':
                    receipt = conversation.get('handled', {}).get(str(source_id), {})
                    valid = receipt.get('owner') is True and str(receipt.get('reply', '')).isdigit()
                    last_key = 'last_affection_reply_id'
                else:
                    raise ValueError('Unknown reply channel')
                if not valid:
                    raise ValueError('没有对应主人回复的成功回执，不能更新好感度')
                key = channel + ':' + str(source_id)
                applied = state.setdefault('affection_applied', {})
                if key not in applied and str(state.get(last_key)) != str(source_id):
                    state['affection'] = max(-100, min(100, int(state['affection']) + delta))
                    state[last_key] = str(source_id)
                    state['updated_at'] = now.isoformat()
                    applied[key] = delta
                    atomic(path, state)
            elif mode != 'read':
                raise ValueError('人格文件不接受任意覆盖')
            return {'ok': True, 'document': document, 'affection': state['affection'],
                    'owner_uid': c.OWNER_UID, 'updated_at': state.get('updated_at')}
        if mode == 'owner_reply' or (mode == 'append' and document == 'task'):
            raise ValueError('Read-only document')
        previous = path.read_text(encoding='utf-8') if path.exists() else ''
        if mode == 'append':
            if not isinstance(content, str) or not 0 < len(content) <= 60000:
                raise ValueError('记录正文必须为1到60000字符')
            if content.strip() and content.strip() not in previous:
                path.parent.mkdir(exist_ok=True, parents=True)
                temp = path.with_suffix(f'.{os.getpid()}.tmp')
                temp.write_text(previous.rstrip()+'\n\n'+content.strip()+'\n', encoding='utf-8')
                os.replace(temp, path)
                previous = path.read_text(encoding='utf-8')
        result = {'ok': True, 'document': document, 'exists': path.exists(),
                  'content': previous[-80000:].replace('<家目录>', str(HOME))}
        if document == 'seen':
            result['authoritative_attempted_bvids'] = list(conversation.get('video_attempts', {}))
        return result
