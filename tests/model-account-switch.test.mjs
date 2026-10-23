import {test} from 'node:test'
import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs'
import {join} from 'node:path'
import {tmpdir} from 'node:os'
const {createAccountSwitch,accountSwitchActive,verifyAccount}=await import(process.env.BILI_TEST_SWITCH_MODULE||new URL('../lib/model-account-switch.mjs',import.meta.url))

function fixture(t){
  const root=mkdtempSync(join(tmpdir(),'model-account-'));t.after(()=>rmSync(root,{recursive:true,force:true}))
  const home=join(root,'含空格 的目录');mkdirSync(home)
  for(const name of ['conversations.json','persona-state.json','cost-ledger.json','bili_credential.json'])writeFileSync(join(home,name),'unmodified fixture '+name)
  return home
}

test('manual switch drains active jobs, deduplicates windows and reveals no session token in status',t=>{
  const home=fixture(t);let busy=true
  const manager=createAccountSwitch(home,{busy:()=>busy})
  const job=manager.begin();assert.equal(job.started,true);assert.equal(accountSwitchActive(home),true)
  assert.equal(manager.ready(job.id,job.token).ready,false)
  assert.equal(manager.begin().started,false)
  assert.equal('token' in manager.status(),false);assert.equal('id' in manager.status(),false)
  busy=false;assert.equal(manager.ready(job.id,job.token).ready,true)
  assert.equal(manager.status().phase,'login')
})

test('wrong capabilities and unverified logins cannot resume pending work',t=>{
  const home=fixture(t),manager=createAccountSwitch(home),job=manager.begin()
  assert.throws(()=>manager.cancel(job.id,'forged'))
  assert.throws(()=>manager.finish(job.id,job.token,{success:true,model:'fixture-model'}))
  manager.ready(job.id,job.token)
  assert.throws(()=>manager.finish(job.id,job.token,{success:false}))
  assert.equal(manager.status().active,true)
})

test('successful verification resumes work while retaining sessions, identity, memory and cost ledger bytes',t=>{
  const home=fixture(t),manager=createAccountSwitch(home),job=manager.begin()
  const names=['conversations.json','persona-state.json','cost-ledger.json','bili_credential.json']
  const before=names.map(name=>readFileSync(join(home,name)))
  manager.ready(job.id,job.token)
  const status=manager.finish(job.id,job.token,{success:true,model:'fixture-model'})
  assert.equal(status.active,false);assert.equal(status.phase,'completed');assert.equal(status.manualOnly,true)
  assert.equal(readFileSync(join(home,'model-account-switch.json'),'utf8').includes(job.token),false)
  names.forEach((name,i)=>assert.deepEqual(readFileSync(join(home,name)),before[i]))
})

test('cancel, restart and expiry preserve the queue and do not select another account automatically',t=>{
  const home=fixture(t);let now=1000
  const manager=createAccountSwitch(home,{now:()=>now}),job=manager.begin()
  const restarted=createAccountSwitch(home,{now:()=>now})
  assert.equal(restarted.status().phase,'draining')
  assert.equal(restarted.cancel(job.id,job.token).active,false)
  const second=manager.begin();now=second.expiresAt+1
  assert.equal(manager.status().phase,'expired');assert.equal(accountSwitchActive(home,now),false)
  assert.throws(()=>manager.ready(second.id,second.token))
  assert.equal(manager.begin().started,true)
  assert.equal(manager.releaseClosedWindow().active,false)
  assert.equal(manager.status().phase,'cancelled')
  const third=manager.begin();manager.ready(third.id,third.token);manager.releaseClosedWindow()
  assert.throws(()=>manager.finish(third.id,third.token,{success:true,model:'fixture-model'}))
})

test('verification uses the chosen configured Gemini model and no Bilibili tools or paid fallback',async t=>{
  const home=fixture(t);writeFileSync(join(home,'model-routing.json'),JSON.stringify({primaryTransport:'antigravity-cli',antigravityCli:'fixture-cli',antigravityModel:'fixture-flash'}))
  let calls=0
  const result=await verifyAccount(home,{assertSettings(){},async runCli(options){
    calls++;assert.equal(options.binary,'fixture-cli');assert.equal(options.model,'fixture-flash')
    assert.deepEqual(JSON.parse(readFileSync(join(options.cwd,'.agents','mcp_config.json'),'utf8')),{})
    assert.ok(options.prompt.includes('不调用工具'))
    return {model:'fixture-flash',response:'OK'}
  }})
  assert.equal(result.success,true);assert.equal(calls,1)
  await assert.rejects(()=>verifyAccount(home,{assertSettings(){throw Error('credits enabled')},async runCli(){throw Error('must not run')}}),/credits enabled/)
  await assert.rejects(()=>verifyAccount(home,{assertSettings(){},async runCli(){return {model:'different',response:'OK'}}}),/VERIFICATION_FAILED/)
})
