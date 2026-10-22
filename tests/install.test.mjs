import {test} from 'node:test'
import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,existsSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join,resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {configure} from '../scripts/configure.mjs'
import {identity} from '../lib/identity.mjs'
import {companionPersona,buildReplyPrompt} from '../lib/persona.mjs'
import {saveCredential,saveOwnerHistoryCredential} from '../lib/login.mjs'
import {prepareOwnerWorkspace} from '../lib/owner-assistant.mjs'
const repo=fileURLToPath(new URL('..',import.meta.url))
function fixture(t){
 const root=mkdtempSync(join(tmpdir(),'whale-portable-'));t.after(()=>rmSync(root,{recursive:true,force:true}))
 const home=join(root,'含空格 的目录','bili-duty');mkdirSync(home,{recursive:true})
 const options={repo,home,ownerUid:'900001',botUid:'900002',ownerName:'测试主人',botName:'测试鲸鱼',appPath:join(root,'宿主','DeepSeek Harness.exe'),nodePath:'fixture-node',cliPath:'fixture-cli',agyPath:''}
 return {root,home,options}
}
test('clean installation uses chosen identities and copies the complete runtime',t=>{
 const {home,options}=fixture(t);configure(options)
 assert.deepEqual(identity(home),{ownerUid:'900001',botUid:'900002',ownerName:'测试主人',botName:'测试鲸鱼'})
 for(const file of ['mcp_server.py','runtime_identity.py','character_memory.py','requirements.txt','AGENTS.md','desktop-dsh.cjs','character-memory.json'])assert.ok(existsSync(join(home,file)),file)
 const cfg=JSON.parse(readFileSync(join(home,'config.json')))
 assert.equal(cfg.dailySpendLimitCny,2);assert.equal(cfg.ownerTaskEnabled,false);assert.equal(cfg.unlimitedVideos,true)
 const prompt=buildReplyPrompt(home);assert.ok(prompt.includes('900001'));assert.ok(prompt.includes('900002'))
 assert.ok(companionPersona(home).includes('测试鲸鱼'))
 assert.ok(!existsSync(join(home,'bili_credential.json')));assert.ok(!existsSync(join(home,'cost-ledger.json')))
})
test('reinstall preserves sessions, custom memories, affection and ledger bytes',t=>{
 const {home,options}=fixture(t);configure(options)
 const files={'conversations.json':'{"sent":{"old":"keep"}}','cost-ledger.json':'{"days":{"day":{"spentCny":1}},"pending":{}}','persona-state.json':'{"affection":67,"custom":"keep"}',
  'AGENTS.md':'我自己的角色约定','character-memory.json':'{"characters":{"mine":{}}}'}
 for(const [name,text] of Object.entries(files))writeFileSync(join(home,name),text)
 configure(options)
 for(const [name,text] of Object.entries(files))assert.equal(readFileSync(join(home,name),'utf8'),text,name)
 assert.throws(()=>configure({...options,ownerUid:'900003'}),/已绑定其他账号/)
})
test('invalid or unbound identity grants no owner and refuses conflicting old credentials',t=>{
 const {home,options}=fixture(t)
 assert.equal(identity(home).ownerUid,'')
 assert.throws(()=>configure({...options,ownerUid:'同名主人'}),/UID/)
 assert.throws(()=>configure({...options,botUid:options.ownerUid}),/UID/)
 writeFileSync(join(home,'bili_credential.json'),JSON.stringify({dedeuserid:'900099',sessdata:'fixture-only'}))
 assert.throws(()=>configure(options),/另一账号/)
 assert.ok(!existsSync(join(home,'identity.json')))
})
test('login cannot replace posting or history account with a different UID',async t=>{
 const {home,options}=fixture(t);configure(options)
 assert.throws(()=>saveCredential(home,{dedeuserid:'900099'}),/绑定的机器人账号/)
 assert.ok(!existsSync(join(home,'bili_credential.json')))
 saveCredential(home,{dedeuserid:'900002',sessdata:'fixture-only'})
 const oldFetch=globalThis.fetch;t.after(()=>{globalThis.fetch=oldFetch})
 globalThis.fetch=async()=>({json:async()=>({code:0,data:{mid:900099}})})
 await assert.rejects(saveOwnerHistoryCredential(home,{sessdata:'fixture-only'}),/主账号/)
 assert.ok(!existsSync(join(home,'owner-history-credential.json')))
 globalThis.fetch=async()=>({json:async()=>({code:0,data:{mid:900001}})})
 await saveOwnerHistoryCredential(home,{sessdata:'fixture-only'})
 assert.equal(JSON.parse(readFileSync(join(home,'owner-history-credential.json'))).dedeuserid,'900001')
})
test('unknown Bilibili goals retain the same full agent environment',t=>{
 const {home,options}=fixture(t);configure(options)
 const job={task_id:'a'.repeat(24),token:'b'.repeat(32),scope:'full',domain:'bilibili'}
 const result=prepareOwnerWorkspace(home,job,{})
 assert.equal(result.scope,'full');assert.ok(result.tools.includes('bili_owner_browser'))
 assert.ok(existsSync(join(result.workspace,'ENVIRONMENT.md')))
 assert.ok(!readFileSync(join(result.workspace,'AGENTS.md'),'utf8').includes('本轮scope=bili'))
 const mcp=JSON.parse(readFileSync(join(result.workspace,'.agents','mcp_config.json')))
 assert.equal(mcp.mcpServers[result.server].env.BILI_DUTY_HOME,home)
})
