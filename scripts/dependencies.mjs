import {spawn} from 'node:child_process'
import {existsSync} from 'node:fs'
import {join} from 'node:path'
import {ensureUv} from '../lib/uvfetch.mjs'
const home=process.argv[2]
const run=(binary,args)=>new Promise((resolve,reject)=>{const child=spawn(binary,args,{stdio:'inherit',windowsHide:true,shell:false});child.on('error',reject);child.on('close',code=>code===0?resolve():reject(Error('依赖安装失败，退出码 '+code)))})
try{
 const uv=await ensureUv({dir:home,log:console.log})
 if(!uv)throw Error('uv未就绪，可在插件设置中重试“装主依赖”')
 const python=join(home,'.venv','Scripts','python.exe')
 if(!existsSync(python))await run(uv,['venv','--seed','--python','3.12',join(home,'.venv')])
 await run(uv,['pip','install','--python',python,'-r',join(home,'requirements.txt')])
 await run(python,['-c','import bilibili_api,mcp,httpx,PIL,playwright,imageio_ffmpeg; print("Python依赖就绪")'])
}catch(error){console.error(error.message);process.exitCode=1}
