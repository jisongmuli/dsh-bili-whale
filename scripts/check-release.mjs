import {readdirSync,readFileSync,statSync} from 'node:fs'
import {join,relative} from 'node:path'
import {fileURLToPath} from 'node:url'
const root=fileURLToPath(new URL('..',import.meta.url));let count=0;const issues=[]
const runtimeNames=new Set(['bili_credential.json','conversations.json','persona-state.json','cost-ledger.json','identity.json','installation.json','owner-watch-history.json','network-settings.json'])
function walk(dir){for(const entry of readdirSync(dir,{withFileTypes:true})){
 if(['.git','node_modules','.test-output','__pycache__'].includes(entry.name))continue
 const file=join(dir,entry.name),name=relative(root,file).replaceAll('\\','/')
 if(entry.isDirectory()){if(['private-images','owner-tasks','video-cache','memes'].includes(entry.name))issues.push(name);else walk(file);continue}
 count++
 if(runtimeNames.has(entry.name)||/credential.*\.json$|\.env$|\.(?:exe|pyd|mp4|jpg|png|webp)$/i.test(entry.name))issues.push('runtime/private file: '+name)
 if(statSync(file).size>2e6)issues.push('unexpected large file: '+name)
 const text=readFileSync(file,'utf8')
 if(/AIza[\w-]{25,}|gh[pousr]_[\w]{30,}|github_pat_[\w_]{30,}|sk-[\w-]{24,}/.test(text))issues.push('possible credential: '+name)
 if(/[A-Za-z]:[/\\]+Users[/\\]+(?!<|\$|%)[a-zA-Z0-9_-]+[/\\]/.test(text))issues.push('personal home path: '+name)
}}
walk(root)
if(issues.length){console.error(issues.join('\n'));process.exitCode=1}else console.log(`Release scan passed: ${count} source/documentation files, no runtime data or obvious credentials.`)
