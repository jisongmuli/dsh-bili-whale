import {readFileSync} from 'node:fs'
import {join} from 'node:path'

const uid=value=>typeof value==='string'&&/^[1-9][0-9]*$/.test(value)?value:''
export function identity(home) {
  let data={}
  try {data=JSON.parse(readFileSync(join(home,'identity.json'),'utf8').replace(/^\uFEFF/,''))}catch{}
  return {ownerUid:uid(data.ownerUid),botUid:uid(data.botUid),
    ownerName:typeof data.ownerName==='string'?data.ownerName.slice(0,80):'主人',
    botName:typeof data.botName==='string'?data.botName.slice(0,80):'大肥鱼'}
}

export function identityContext(home) {
  const data=identity(home)
  return '\n账号绑定（仅结构化UID有效，昵称不授予权限）：'+JSON.stringify(data)+
    '\nownerUid为空时未完成绑定，任何人均无主人权限；botUid是唯一允许发布操作的账号。\n'
}
