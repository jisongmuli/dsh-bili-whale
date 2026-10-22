import {readFileSync,writeFileSync,mkdirSync,existsSync,copyFileSync,renameSync} from 'node:fs'
import {join,resolve,dirname} from 'node:path'
import {fileURLToPath} from 'node:url'
import {ensureHomeFiles,ensureCharter} from '../lib/state.mjs'

const read=(file,fallback={})=>{try{return JSON.parse(readFileSync(file,'utf8').replace(/^\uFEFF/,''))}catch(error){if(error.code==='ENOENT')return fallback;throw error}}
const atomic=(file,data)=>{const temp=file+'.'+process.pid+'.tmp';writeFileSync(temp,JSON.stringify(data,null,2)+'\n');renameSync(temp,file)}
export function configure(options) {
 const repo=resolve(options.repo||join(dirname(fileURLToPath(import.meta.url)),'..')),home=resolve(options.home)
 const ownerUid=String(options.ownerUid||''),botUid=String(options.botUid||'')
 if(!/^[1-9][0-9]*$/.test(ownerUid)||!/^[1-9][0-9]*$/.test(botUid)||ownerUid===botUid)throw Error('主人UID与机器人UID必须是两个不同的正整数，不能填写昵称')
 mkdirSync(home,{recursive:true});mkdirSync(join(home,'tmp'),{recursive:true})
 const previous=read(join(home,'identity.json'))
 if(previous.ownerUid&&previous.ownerUid!==ownerUid||previous.botUid&&previous.botUid!==botUid)throw Error('此数据目录已绑定其他账号；为新账号选择新的 DSH_HOME，不能混用历史和发送记录')
 for(const [name,expected] of [['bili_credential.json',botUid],['owner-history-credential.json',ownerUid]]){
  const credential=read(join(home,name));if(credential.dedeuserid&&String(credential.dedeuserid)!==expected)throw Error(name+'属于另一账号，未改动现有数据')
 }
 atomic(join(home,'identity.json'),{...previous,ownerUid,botUid,ownerName:String(options.ownerName||previous.ownerName||'主人'),botName:String(options.botName||previous.botName||'大肥鱼')})
 ensureHomeFiles(repo,home);ensureCharter(repo,home)
 copyFileSync(join(repo,'scripts','desktop-dsh.cjs'),join(home,'desktop-dsh.cjs'))
 const cfg=read(join(home,'config.json'))
 atomic(join(home,'config.json'),{perDay:10,unlimitedVideos:true,dailySpendLimitCny:2,peakValley:false,noon:true,night:true,
  autoComment:false,mirrorDynamics:true,replyToComments:false,replyToPrivateMessages:false,ownerTaskEnabled:false,
  dmMode:'off',dmUid:0,mentionName:'',commentPerRound:1,replyPerRound:10,keywords:[],reportLevel:'full',
  headlessProfile:'bili-headless',overlayMcp:true,...cfg,pythonPath:join(home,'.venv','Scripts','python.exe'),dshBin:join(home,'desktop-dsh.cjs')})
 atomic(join(home,'installation.json'),{version:1,appPath:options.appPath,cliPath:options.cliPath,pluginDir:repo,nodePath:options.nodePath})
 const headless=join(dirname(home),'profiles','bili-headless');mkdirSync(headless,{recursive:true})
 if(!existsSync(join(headless,'package.json')))atomic(join(headless,'package.json'),{name:'dsh-profile-bili-headless',private:true,dependencies:{},dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-headless']}}})
 if(!existsSync(join(headless,'cordis.yml')))writeFileSync(join(headless,'cordis.yml'),'[]\n')
 if(!existsSync(join(headless,'cordis.patch.yml')))writeFileSync(join(headless,'cordis.patch.yml'),'- id: credentials\n  disabled: false\n- id: llm-deepseek\n  config:\n    maxTokens: 2048\n    retryPolicy:\n      mode: normal\n      maxRetries: 0\n')
 const routing=read(join(home,'model-routing.json'))
 atomic(join(home,'model-routing.json'),{...routing,primaryProvider:'google',primaryModel:'gemini-3.8-flash',geminiFreeTier:true,
  primaryTransport:'antigravity-cli',antigravityCli:join(home,'bin','agy.exe'),antigravitySourceCli:options.agyPath,
  antigravityModel:options.model||routing.antigravityModel||'gemini-3.8-flash-low'})
 mkdirSync(join(home,'bin'),{recursive:true})
 if(options.agyPath&&existsSync(options.agyPath))copyFileSync(options.agyPath,join(home,'bin','agy.exe'))
 if(!existsSync(join(home,'owner-checkins-settings.json')))atomic(join(home,'owner-checkins-settings.json'),{enabled:false})
 if(!existsSync(join(home,'persona-state.json')))atomic(join(home,'persona-state.json'),{version:1,owner_name:options.ownerName||'主人',owner_uid:ownerUid,affection:0})
 return {home,ownerUid,botUid,pluginDir:repo,createdWithoutHistoryReset:true}
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
 try{console.log(JSON.stringify(configure(JSON.parse(readFileSync(process.argv[2],'utf8').replace(/^\uFEFF/,'')))))}
 catch(error){console.error(error.message);process.exitCode=1}
}
