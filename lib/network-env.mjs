import {readFileSync} from 'node:fs'
import {join} from 'node:path'

// HTTPX versions used by the MCP reject bare/bracketed IPv6 NO_PROXY entries.
// AGY can inject these even when the desktop's own environment has none.
export function safeNoProxy(value='') {
  return [...new Set([...String(value).split(','),'localhost','127.0.0.1','.bilibili.com','.biliapi.net','.hdslb.com','.biliimg.com','.bilivideo.com','api.deepseek.com']
    .map(s=>s.trim()).filter(s=>s==='*'||/^\.?[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*(?::\d+)?$/.test(s)))].join(',')
}

export function modelNetworkEnv(home) {
  try {
    const settings=JSON.parse(readFileSync(join(home,'network-settings.json'),'utf8'))
    const proxy=settings.proxy?.https || settings.proxy?.http
    if(!proxy)return {}
    const url=new URL(proxy)
    if(!['127.0.0.1','localhost','[::1]'].includes(url.hostname) || !['http:','https:'].includes(url.protocol))return {}
    return {NODE_USE_ENV_PROXY:'1',HTTPS_PROXY:proxy,HTTP_PROXY:proxy,
      NO_PROXY:safeNoProxy(process.env.NO_PROXY||process.env.no_proxy),no_proxy:safeNoProxy(process.env.NO_PROXY||process.env.no_proxy)}
  }catch{return {}}
}
