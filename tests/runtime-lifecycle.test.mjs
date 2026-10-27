import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdtempSync,writeFileSync,readFileSync,existsSync,utimesSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join,resolve,sep} from 'node:path'
import {spawnSync} from 'node:child_process'
import {pathToFileURL,fileURLToPath} from 'node:url'
const modulePath=process.env.BILI_LEASE_MODULE||fileURLToPath(new URL('../lib/runtime-lease.mjs',import.meta.url))
const {claimRuntime}=await import(pathToFileURL(modulePath))
const make=t=>{const home=mkdtempSync(join(tmpdir(),'whale-lease-'));t.after(()=>{assert.ok(resolve(home).startsWith(resolve(tmpdir())+sep));rmSync(home,{recursive:true,force:true})});return home}
const save=(home,name,data)=>writeFileSync(join(home,name),JSON.stringify(data))
test('one owner, idempotent release and old callbacks do not release a successor',t=>{
 const home=make(t),release=claimRuntime(home);assert.equal(typeof release,'function');assert.equal(claimRuntime(home),null)
 release();const next=claimRuntime(home);assert.equal(typeof next,'function');release();assert.equal(claimRuntime(home),null)
 next();assert.equal(JSON.parse(readFileSync(join(home,'runtime-host.lifecycle.json'),'utf8')).phase,'stopped')
})
test('a stopped plugin in a still-live old host no longer blocks another host',t=>{
 const home=make(t)
 save(home,'runtime-status.json',{runtimePid:process.ppid})
 save(home,'runtime-host.lifecycle.json',{phase:'stopped',pid:process.ppid})
 const release=claimRuntime(home);assert.equal(typeof release,'function');release()
})
test('a live legacy host without a lifecycle is still respected',t=>{
 const home=make(t);save(home,'runtime-status.json',{runtimePid:process.ppid});assert.equal(claimRuntime(home),null)
})
test('fresh partial locks wait, abandoned partial locks recover',t=>{
 const home=make(t),file=join(home,'runtime-host.lock.json');writeFileSync(file,'')
 assert.equal(claimRuntime(home),null);utimesSync(file,new Date(0),new Date(0))
 const release=claimRuntime(home);assert.equal(typeof release,'function');release()
})
test('partial metadata never overrides a known live modern owner',t=>{
 const home=make(t),file=join(home,'runtime-host.lock.json');writeFileSync(file,'');utimesSync(file,new Date(0),new Date(0))
 save(home,'runtime-host.lifecycle.json',{phase:'active',pid:process.ppid});assert.equal(claimRuntime(home),null)
})
test('a missing file does not override an active lifecycle',t=>{
 const home=make(t);save(home,'runtime-host.lifecycle.json',{phase:'active',pid:process.ppid});assert.equal(claimRuntime(home),null)
})
test('OS process death releases logical ownership and keeps history intact',t=>{
 const home=make(t);save(home,'conversations.json',{sent:{old:'receipt'}})
 const script=`import {claimRuntime} from ${JSON.stringify(pathToFileURL(modulePath).href)};const release=claimRuntime(${JSON.stringify(home)});if(!release)process.exit(2);process.exit(0)`
 const child=spawnSync(process.execPath,['--input-type=module','-e',script],{encoding:'utf8',windowsHide:true});assert.equal(child.status,0,child.stderr)
 const release=claimRuntime(home);assert.equal(typeof release,'function');assert.deepEqual(JSON.parse(readFileSync(join(home,'conversations.json'),'utf8')),{sent:{old:'receipt'}});release()
})
