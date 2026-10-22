'use strict';
const {readFileSync}=require('node:fs');
const {join,dirname}=require('node:path');
const {pathToFileURL}=require('node:url');
const {spawn}=require('node:child_process');
const home=__dirname;
const install=JSON.parse(readFileSync(join(home,'installation.json'),'utf8').replace(/^\uFEFF/,''));
const runHarness=args=>new Promise(resolve=>{
 const child=spawn(install.appPath,['--expose-internals','--require',join(install.pluginDir,'lib','model-preload.cjs'),join(install.pluginDir,'scripts','host-cli.cjs'),install.cliPath,...args],
  {stdio:'inherit',windowsHide:true,env:{...process.env,ELECTRON_RUN_AS_NODE:'1'}});
 child.on('error',error=>{console.error('DSH_START_'+(error.code||'ERROR'));resolve(1)});
 child.on('close',code=>resolve(code??1));
});
import(pathToFileURL(join(install.pluginDir,'lib','antigravity-duty.mjs')).href)
 .then(({runDutyWithAntigravity})=>runDutyWithAntigravity({home,args:process.argv.slice(2),runHarness}))
 .then(code=>{process.exitCode=code})
 .catch(error=>{console.error('Whale runner: '+error.message);process.exitCode=1});
