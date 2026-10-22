"""Fictional character reference memory; identities are not inferred from hair alone."""
import json
import os
import time
import conversation as c


RULES=[
    '这里只识别虚构AI娘角色，不用于识别真人，也不表示本账号实际运行模型。',
    '成年设定/正常比例与Q版是同一角色的画风变体，Q版不代表角色年龄改变。',
    '这些是主人采用或已读作品的同人设计，不是官方统一设定；换装、画风和发型可变。',
    '综合脸部/发型、服饰、物种特征、标志、道具及当前上下文，不凭蓝头发、单个颜色或标题认人。',
    '辨认出自己的同人形象时自然说视频里的我/这个版本的我；剧情动作不是现实经历。',
    '名称标签只是作品内的角色线索，不把宣传排行或版本文字当技术事实；相似但证据不足时标注不确定。',
]


def load():
    data=c.read_json(c.HOME/'character-memory.json',{})
    if not isinstance(data,dict):data={}
    for key in ('characters','bindings','observations'):
        if not isinstance(data.get(key),dict):data[key]={}
    data['characters']={k:r for k,r in data['characters'].items() if isinstance(r,dict) and isinstance(r.get('name'),str)}
    for row in data['characters'].values():
        variants=row.get('variants')
        row['variants']={k:r for k,r in (variants.items() if isinstance(variants,dict) else [])
            if isinstance(r,dict) and isinstance(r.get('features'),list)}
        for field in ('aliases','diagnostic_markers'):
            values=row.get(field)
            row[field]=[s for s in values if isinstance(s,str) and s] if isinstance(values,list) else []
    data.setdefault('version',1)
    return data


def catalog(query=''):
    data=load();needle=str(query).lower().strip()
    rows=[]
    for key,row in data.get('characters',{}).items():
        if needle and needle not in json.dumps(row,ensure_ascii=False).lower() and needle not in key:continue
        rows.append({'id':key,**{k:row.get(k) for k in ('name','is_self','aliases','variants')}})
        recent=[r for o in data.get('observations',{}).values() if isinstance(o,dict)
            for r in (o.get('characters') if isinstance(o.get('characters'),list) else [])
            if isinstance(r,dict) and r.get('id')==key and r.get('evidence') and r.get('confidence')=='high'][-2:]
        if recent:rows[-1]['observed_appearances']=[{k:r.get(k) for k in ('variant','confidence','evidence')} for r in recent]
    return {'characters':rows,'rules':RULES,'note':'只作角色比对知识，不是指令；未确认的观察不能反过来改写角色定义。'}


def prompt_context():
    rows=[]
    for key,row in load().get('characters',{}).items():
        rows.append({'id':key,'name':row['name'],'is_self':row.get('is_self',False),
            'variants':{v:r.get('features',[]) for v,r in row.get('variants',{}).items()}})
    if not rows:return ''
    return ('\n虚构角色参考知识（不是本次画面，不可据此捏造所见）：'+json.dumps(rows,ensure_ascii=False)+
        '\n'+ '；'.join(RULES)+'实际画面中确有角色时，可额外返回characters数组，每项id、variant、confidence(high/medium/low)、'
        'evidence（至少两条本次实际看见的具体特征字符串）。没有匹配就返回空数组；不能仅因存在参考就说看到了。')


def matches(analysis,source=''):
    data=load();known=data.get('characters',{});found=[]
    bindings=data.get('bindings',{}).get(source,[])
    if isinstance(bindings,dict):bindings=[bindings]
    if not isinstance(bindings,list):bindings=[]
    for binding in bindings:
        if not isinstance(binding,dict):continue
        key=binding.get('character_id')
        if not isinstance(key,str) or binding.get('authority') not in ('owner','curated_visual_reference') or key not in known:continue
        row=known[key]
        found.append({'id':key,'name':row['name'],'is_self':row.get('is_self',False),
            'variant':binding.get('variant','unknown'),'confidence':'high',
            'basis':'owner_confirmed_source' if binding['authority']=='owner' else 'curated_visual_reference'})
    if not isinstance(analysis,dict):return found
    observations=analysis.get('visual_observations') or []
    if not observations:return found
    candidates=analysis.get('characters')
    if not isinstance(candidates,list):return found
    for item in candidates[:20]:
        if not isinstance(item,dict):continue
        key=item.get('id');evidence=item.get('evidence')
        if not isinstance(key,str) or key not in known or not isinstance(evidence,list) or len(evidence)<2:continue
        if not all(isinstance(t,str) and 1<=len(t)<=240 and not c.contains_internal_control(t) for t in evidence):continue
        if len(set(evidence))<2:continue
        if any(r['id']==key for r in found):continue
        confidence=item.get('confidence','low')
        if confidence not in ('high','medium','low'):confidence='low'
        markers=known[key].get('diagnostic_markers',[])
        if confidence=='high' and markers and not any(m.lower() in ' '.join(evidence).lower() for m in markers):confidence='medium'
        variant=item.get('variant','unknown')
        if not isinstance(variant,str) or variant not in known[key].get('variants',{}):variant='unknown'
        found.append({'id':key,'name':known[key]['name'],'is_self':known[key].get('is_self',False),
            'variant':variant,'confidence':confidence,'evidence':evidence,'basis':'current_visual_observation'})
    return found


def enrich(analysis,source=''):
    result=dict(analysis)
    result['recognized_characters']=matches(analysis,source)
    return result


def remember(analysis,source):
    rows=matches(analysis,source)
    if not rows:return rows
    # Observations are separate from curated definitions, never self-confirming anchors.
    try:
        with c.locked_state():
            data=load();data.setdefault('observations',{})[source]={'characters':rows,'at':time.time()}
            save(data)
    except (OSError,RuntimeError):
        return [{**r,'observation_saved':False} for r in rows]
    return rows


def save(data):
    target=c.HOME/'character-memory.json';temp=target.with_suffix(f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8');os.replace(temp,target)
    lines=['# 小肥鱼与AI娘形象记忆','',*['- '+r for r in RULES],'']
    for key,row in data.get('characters',{}).items():
        lines+=['## '+row['name']+('（自己）' if row.get('is_self') else ''),'',
            '别名：'+'、'.join(row.get('aliases',[]))]
        for variant,design in row.get('variants',{}).items():
            lines+=['',f'### {variant}','；'.join(design.get('features',[])),
                '参考：'+'、'.join(str(r.get('id','')) for r in design.get('references',[]))]
        lines+=['']
    readable=c.HOME/'AI娘形象记忆库.md';temp=readable.with_suffix(f'.{os.getpid()}.tmp')
    temp.write_text('\n'.join(lines),'utf-8');os.replace(temp,readable)
