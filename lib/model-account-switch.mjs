import {mkdirSync,readFileSync,writeFileSync,renameSync,existsSync} from 'node:fs'
import {join,resolve} from 'node:path'
import {randomBytes} from 'node:crypto'
import {fileURLToPath} from 'node:url'
import {assertSubscriptionSettings,runAntigravity} from './antigravity-cli.mjs'
import {modelNetworkEnv} from './network-env.mjs'

const read=file=>{try{return JSON.parse(readFileSync(file,'utf8').replace(/^\uFEFF/,''))}catch(error){if(error.code==='ENOENT')return {};throw error}}
const fileOf=home=>join(home,'model-account-switch.json')
function save(home,value){const file=fileOf(home),tmp=file+'.'+process.pid+'.tmp';writeFileSync(tmp,JSON.stringify(value,null,2));renameSync(tmp,file)}
export function accountSwitchActive(home,now=Date.now()){
  const row=read(fileOf(home))
  return ['draining','login'].includes(row.phase)&&row.expiresAt>now
}

export function createAccountSwitch(home,{busy=()=>false,now=Date.now}={}){
  const status=()=>{
    const row=read(fileOf(home)),active=accountSwitchActive(home,now())
    return {active,phase:active?row.phase:row.phase==='draining'||row.phase==='login'?'expired':row.phase||'idle',
      busy:Boolean(busy()),startedAt:row.startedAt||null,expiresAt:row.expiresAt||null,
      verifiedAt:row.verifiedAt||null,model:row.model||null,manualOnly:true}
  }
  const authenticated=(id,token)=>{
    const row=read(fileOf(home))
    if(!id||!token||row.id!==id||row.token!==token||!accountSwitchActive(home,now()))throw Error('MODEL_SWITCH_SESSION_INVALID')
    return row
  }
  return {status,
    begin(){
      if(accountSwitchActive(home,now()))return {started:false,...status()}
      const row={id:randomBytes(12).toString('hex'),token:randomBytes(24).toString('hex'),phase:'draining',startedAt:now(),expiresAt:now()+45*60*1000}
      save(home,row);return {started:true,...row}
    },
    ready(id,token){
      const row=authenticated(id,token)
      if(busy())return {ready:false,...status()}
      row.phase='login';row.expiresAt=now()+45*60*1000;save(home,row)
      return {ready:true,...status()}
    },
    finish(id,token,verification){
      const row=authenticated(id,token)
      if(row.phase!=='login'||verification?.success!==true||typeof verification.model!=='string')throw Error('MODEL_SWITCH_NOT_VERIFIED')
      save(home,{...row,token:undefined,phase:'completed',finishedAt:now(),verifiedAt:now(),model:verification.model})
      return status()
    },
    cancel(id,token){
      const row=authenticated(id,token)
      save(home,{...row,token:undefined,phase:'cancelled',finishedAt:now()});return status()
    },
    releaseClosedWindow(){
      const row=read(fileOf(home))
      if(accountSwitchActive(home,now()))save(home,{...row,token:undefined,phase:'cancelled',finishedAt:now(),releasedFromSettings:true})
      return status()
    },
  }
}

export function accountLoginConfig(home,{fileExists=existsSync}={}){
  const routing=read(join(home,'model-routing.json'))
  if(routing.primaryTransport!=='antigravity-cli')throw Error('SUBSCRIPTION_CLI_NOT_CONFIGURED')
  // Interactive login uses the installed official CLI, which receives vendor
  // updates independently of the pinned background worker binary.
  const binary=[routing.antigravitySourceCli,routing.antigravityCli].find(path=>path&&fileExists(path))
  if(!binary)throw Error('SUBSCRIPTION_CLI_NOT_FOUND')
  const {NODE_USE_ENV_PROXY,...env}=modelNetworkEnv(home)
  return {binary,env}
}

export async function verifyAccount(home,{runCli=runAntigravity,assertSettings=assertSubscriptionSettings}={}){
  assertSettings()
  const routing=read(join(home,'model-routing.json'))
  if(routing.primaryTransport!=='antigravity-cli'||!routing.antigravityCli)throw Error('SUBSCRIPTION_CLI_NOT_CONFIGURED')
  const cwd=join(home,'tmp','model-account-verification')
  mkdirSync(join(cwd,'.agents'),{recursive:true})
  writeFileSync(join(cwd,'.agents','mcp_config.json'),'{}')
  writeFileSync(join(cwd,'AGENTS.md'),'只回复OK。不读取文件、不运行命令、不调用工具。')
  const model=routing.antigravityModel||'gemini-3.8-flash-low'
  const result=await runCli({binary:routing.antigravityCli,model,cwd,env:modelNetworkEnv(home),
    prompt:'只回复OK。不调用工具。',timeoutMs:90000})
  if(result.model!==model||!String(result.response||'').trim())throw Error('SUBSCRIPTION_VERIFICATION_FAILED')
  return {success:true,model,verifiedAt:new Date().toISOString()}
}

if(['--verify','--login-config'].includes(process.argv[2])&&resolve(process.argv[1]||'')===fileURLToPath(import.meta.url)){
  try{console.log(JSON.stringify(process.argv[2]==='--login-config'?accountLoginConfig(resolve(process.argv[3])):await verifyAccount(resolve(process.argv[3]))))}
  catch(error){console.log(JSON.stringify({success:false,code:error.code||error.message||'VERIFICATION_FAILED'}));process.exitCode=1}
}
