import {readFileSync,writeFileSync,openSync,closeSync,unlinkSync,existsSync,statSync} from 'node:fs'
import {join} from 'node:path'
import {randomUUID} from 'node:crypto'
const read=file=>{try{return JSON.parse(readFileSync(file,'utf8'))}catch{return {}}}
const alive=pid=>{if(!Number.isSafeInteger(pid)||pid<=0)return false;try{process.kill(pid,0);return true}catch(error){return error.code==='EPERM'}}
export function claimRuntime(home){
  const file=join(home,'runtime-host.lock.json'),token=randomUUID()
  const lifecycle=join(home,'runtime-host.lifecycle.json')
  const life=read(lifecycle)
  // A released plugin may leave its host process alive. Its old status PID
  // must not veto every subsequent activation in another desktop host.
  if(life.phase==='active'&&alive(life.pid)&&!existsSync(file))return null
  // Recognize legacy owners only until this directory has a modern lifecycle.
  const previous=read(join(home,'runtime-status.json')).runtimePid
  if(!existsSync(lifecycle)&&!existsSync(file)&&previous!==process.pid&&alive(previous))return null
  for(let retry=0;retry<2;retry++){
    try{
      const fd=openSync(file,'wx');try{writeFileSync(fd,JSON.stringify({pid:process.pid,token,at:new Date().toISOString()}))}finally{closeSync(fd)}
      try{writeFileSync(lifecycle,JSON.stringify({phase:'active',pid:process.pid,token,at:new Date().toISOString()}))}
      catch(error){if(read(file).token===token)try{unlinkSync(file)}catch{};throw error}
      return ()=>{
        if(read(file).token!==token)return
        // Persist the tombstone before removing the live lease; stale release
        // callbacks cannot release a later owner's token.
        writeFileSync(lifecycle,JSON.stringify({phase:'stopped',pid:process.pid,token,at:new Date().toISOString()}))
        try{unlinkSync(file)}catch{}
      }
    }catch(error){
      if(error.code!=='EEXIST')throw error
      const lease=read(file)
      if(alive(lease.pid))return null
      // Allow a newly created partial file time to finish; an abandoned empty
      // file must not permanently disable the background service after a crash.
      if(!lease.token){
        if(life.phase==='active'&&alive(life.pid))return null
        try{if(Date.now()-statSync(file).mtimeMs<30000)return null}catch{return null}
      }
      try{unlinkSync(file)}catch{return null}
    }
  }
  return null
}
