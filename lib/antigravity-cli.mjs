import {spawn} from 'node:child_process'
import {readFileSync,existsSync} from 'node:fs'
import {join,normalize,dirname} from 'node:path'
import {fileURLToPath} from 'node:url'
import {homedir} from 'node:os'
import {StringDecoder} from 'node:string_decoder'

export function assertSubscriptionSettings(path=join(homedir(),'.gemini','antigravity-cli','settings.json')) {
  const setupFailure=code=>Object.assign(Error(code),{code})
  let settings={}
  try {settings=JSON.parse(readFileSync(path,'utf8').replace(/^\uFEFF/,''))} catch(error) {
    if(error.code!=='ENOENT')throw setupFailure('ANTIGRAVITY_SETTINGS_UNREADABLE')
  }
  // The official CLI omits default false values when it saves settings.
  if(![undefined,false,'off'].includes(settings.useG1Credits))throw setupFailure('ANTIGRAVITY_CREDIT_OVERAGES_MUST_BE_OFF')
  if(settings.modelProvider==='gemini')throw setupFailure('ANTIGRAVITY_ACCOUNT_LOGIN_REQUIRED')
}

export function subscriptionEnv(extra={}) {
  const env={...process.env,...extra}
  for(const name of ['GEMINI_API_KEY','GOOGLE_API_KEY','AGY_ADC_AUTH','GOOGLE_GENAI_USE_VERTEXAI','GOOGLE_GEMINI_BASE_URL'])delete env[name]
  // This flag is for Node's API fetch. Passing it to the native CLI can stall
  // initialization before its first event; standard HTTP(S)_PROXY remains set.
  delete env.NODE_USE_ENV_PROXY
  return env
}

// CLI builds expose MCP calls either through call_mcp_tool or as direct tools.
export function toolName(step={}) {
  if(step.tool_name==='call_mcp_tool')return step.tool_info?.parameters?.ToolName||step.tool_name
  const name=String(step.tool_name||'')
  const at=name.lastIndexOf('_bili_')
  return name.startsWith('mcp_')&&at>=0?name.slice(at+1):name
}

export function stopDutyTree(child) {
  if(!child)return
  if(process.platform==='win32'&&Number.isSafeInteger(child.pid)&&child.pid>0){
    const killer=spawn('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore',shell:false})
    killer.on('error',()=>{try{child.kill()}catch{}})
  }else{try{child.kill()}catch{}}
}

export function runAntigravity({binary,model,cwd,prompt,env={},timeoutMs=20*60*1000,onEvent=()=>{},signal,spawnImpl=spawn,ownerExecution=false}) {
  return new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(Object.assign(Error('Cancelled'),{code:'ABORTED'}));return}
    const args=['--input-format','stream-json','--output-format','stream-json','--model',model,
      '--disable-slash-commands','--print-timeout',`${Math.ceil(timeoutMs/1000)}s`]
    // Per-process only. Public browsing and ordinary chats keep their scoped permissions.
    if(ownerExecution)args.push('--dangerously-skip-permissions','--sandbox=false')
    let child
    // The desktop Electron runtime can fail external Go CLI spawning while the
    // same executable works from regular Node. Use its bundled Node relay.
    const bundled=join(dirname(process.execPath),'resources','runtime','primary-runtime','dependencies','node','bin','node.exe')
    const relay=process.platform==='win32'&&process.versions.electron&&existsSync(bundled)
    const command=relay?bundled:normalize(binary)
    const commandArgs=relay?[fileURLToPath(new URL('./antigravity-spawn.cjs',import.meta.url)),normalize(binary),...args]:args
    try {child=spawnImpl(command,commandArgs,{cwd:normalize(cwd),env:subscriptionEnv(env),shell:false,windowsHide:true,stdio:['pipe','pipe','pipe']})}
    catch(error){reject(error);return}
    let pending='',bytes=0,stderr='',result,actualModel,done=false,timedOut=false,aborted=false
    const decoder=new StringDecoder('utf8')
    const stop=()=>{aborted=true;stopDutyTree(child)}
    signal?.addEventListener('abort',stop,{once:true})
    const timer=setTimeout(()=>{timedOut=true;stopDutyTree(child)},timeoutMs+5000)
    const complete=(error,value)=>{
      if(done)return;done=true;clearTimeout(timer);signal?.removeEventListener('abort',stop)
      if(error)reject(error);else resolve(value)
    }
    const line=text=>{
      if(!text.trim())return
      let event
      try {event=JSON.parse(text)}catch{return}
      if(event.event==='init')actualModel=event.init?.model
      if(event.event==='result')result=event.result
      try {onEvent(event)}catch{}
    }
    child.stdout.on('data',chunk=>{
      bytes+=chunk.length
      if(bytes>16*1024*1024){stopDutyTree(child);complete(Error('ANTIGRAVITY_OUTPUT_TOO_LARGE'));return}
      pending+=decoder.write(chunk)
      let end
      while((end=pending.indexOf('\n'))>=0){line(pending.slice(0,end));pending=pending.slice(end+1)}
    })
    child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString('utf8')).slice(-16000)})
    child.on('error',error=>complete(error))
    child.on('close',code=>{
      line(pending+decoder.end())
      if(aborted){complete(Object.assign(Error('Cancelled'),{code:'ABORTED'}));return}
      if(timedOut){complete(Object.assign(Error('ANTIGRAVITY_TIMEOUT'),{code:'TIMEOUT'}));return}
      if(code!==0||result?.status!=='SUCCESS'||actualModel!==model){
        const message=String(result?.error||stderr||'Missing successful result')
        const kind=/quota|exhaust|rate.limit|resource.?exhaust/i.test(message)?'QUOTA':
          /auth|login|sign.in|eligibility|token|userinfo/i.test(message)?'AUTH':
          /EOF|network|connection|timeout|dial tcp/i.test(message)?'NETWORK':'CLI_ERROR'
        complete(Object.assign(Error(`Antigravity CLI failed (${kind})`),{code:kind,
          diagnostic:{exitCode:code,resultPresent:Boolean(result),status:result?.status||null,actualModel:actualModel||null,expectedModel:model,
            outputBytes:bytes,errorCategory:kind}}));return
      }
      complete(null,{...result,model:actualModel})
    })
    child.stdin.on('error',error=>{if(error.code!=='EPIPE')complete(error)})
    child.stdin.end(JSON.stringify({event:'user',message:{content:prompt}})+'\n')
  })
}
