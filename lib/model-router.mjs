import {readFileSync,writeFileSync,renameSync} from 'node:fs'
import {join} from 'node:path'
import {freeGeminiEnabled} from './budget-guard.mjs'

function record(home, patch) {
  try {
    const file=join(home,'model-router-status.json')
    let previous={}
    try {previous=JSON.parse(readFileSync(file,'utf8'))} catch {}
    const next={...previous,...patch,updatedAt:new Date().toISOString()}
    const temp=file+'.'+process.pid+'.tmp'
    writeFileSync(temp,JSON.stringify(next,null,2));renameSync(temp,file)
  } catch {}
}

export async function* routeGemini(home, options, next, fallback) {
  if (!freeGeminiEnabled(home) || options.provider!=='google' || options.model!=='gemini-3.8-flash') {
    yield* next();return
  }
  const buffered=[]
  let failed=false,finished=false,code='UNKNOWN',aborted=false
  try {
    for await(const chunk of next()) {
      if(chunk.type==='finish') {
        finished=true
        failed=chunk.reason?.kind==='error' || chunk.reason?.kind==='aborted'
        aborted=chunk.reason?.kind==='aborted'
        code=chunk.reason?.failure?.code || chunk.reason?.kind || 'UNKNOWN'
      }
      buffered.push(chunk)
    }
  } catch(error) {failed=true;code=error?.code || 'GEMINI_REQUEST_FAILED'}
  if(!failed && finished) {
    record(home,{lastProvider:'google',lastModel:'gemini-3.8-flash',lastGeminiSucceeded:true,lastGeminiFailureCode:null,fallbackUsed:false})
    yield* buffered;return
  }
  if(aborted || options.signal?.aborted) {
    // User cancellation is not a request to spend money on another provider.
    yield {type:'finish',reason:{kind:'aborted',failure:{code:'ABORTED',message:'Gemini request cancelled'}}};return
  }
  record(home,{lastProvider:'deepseek-official',lastModel:'deepseek-flash',lastGeminiSucceeded:false,
               lastGeminiFailureCode:String(code).slice(0,80),fallbackUsed:true})
  // Buffering prevented partial Gemini tool calls from executing before failure.
  // DeepSeek replay metadata cannot be stored as Google-native replay state.
  for await(const chunk of fallback({...options,provider:'deepseek-official',model:'deepseek-flash',
                                     reasoningEffort:'off',maxTokens:Math.min(options.maxTokens || 2048,2048)})) {
    if(chunk.type==='finish') {
      const {state,replayState,...portable}=chunk
      yield portable
    } else yield chunk
  }
}

export const name='bili-model-router'
export const inject=['llm']
export function apply(ctx,config={}) {
  const home=config.home || process.env.BILI_DUTY_HOME
  if(!home)throw Error('Missing Bilibili model routing home')
  record(home,{loadedAt:new Date().toISOString(),pid:process.pid,primary:'google/gemini-3.8-flash',fallback:'deepseek-official/deepseek-flash'})
  ctx.on('llm/stream',(options,next)=>routeGemini(home,options,next,replacement=>ctx.llm.stream(replacement)),{global:true,prepend:true})
}
