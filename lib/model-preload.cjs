// The Electron CLI does not apply Node's environment proxy initialization.
// Route official Google requests through the user's existing local proxy.
const {createRequire}=require('node:module')
let undici
try {undici=createRequire(require('node:path').join(require('node:path').dirname(process.execPath),'resources','app.asar','dsh','package.json'))('undici')}
catch {undici=require('internal/deps/undici/undici')}
const proxy=process.env.HTTPS_PROXY || process.env.HTTP_PROXY
if(proxy) {
  const direct=new undici.Agent({connectTimeout:30000,headersTimeout:120000,bodyTimeout:300000})
  const viaProxy=new undici.ProxyAgent({uri:proxy,connectTimeout:30000,headersTimeout:120000,bodyTimeout:300000})
  undici.setGlobalDispatcher({dispatch(options,handler){
    const host=new URL(String(options.origin)).hostname
    return (host==='generativelanguage.googleapis.com' || host.endsWith('.googleapis.com') ? viaProxy : direct).dispatch(options,handler)
  }})
}
// Use the official non-stream endpoint and adapt its complete response to SSE.
// This keeps Google's native tool signatures while avoiding stalled proxy SSE.
globalThis.fetch=async function(input,init) {
  const url=new URL(input instanceof undici.Request ? input.url : String(input))
  if(url.hostname==='generativelanguage.googleapis.com' && url.pathname.endsWith(':streamGenerateContent')) {
    url.pathname=url.pathname.replace(':streamGenerateContent',':generateContent')
    url.searchParams.delete('alt')
    let request=init
    if(input instanceof undici.Request)request={method:input.method,headers:input.headers,
      signal:input.signal,body:['GET','HEAD'].includes(input.method)?undefined:await input.arrayBuffer(),...init}
    const response=await undici.fetch(url,request)
    if(!response.ok)return response
    const body=await response.text()
    return new undici.Response('data: '+body.replace(/\r?\n/g,' ')+'\n\n',{
      status:200,headers:{'content-type':'text/event-stream; charset=utf-8'}})
  }
  return undici.fetch(input,init)
}
globalThis.Headers=undici.Headers
globalThis.Request=undici.Request
globalThis.Response=undici.Response
