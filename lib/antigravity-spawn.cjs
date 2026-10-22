// A transparent regular-Node transport for the desktop's authenticated CLI.
const {spawn}=require('node:child_process')
const child=spawn(process.argv[2],process.argv.slice(3),{cwd:process.cwd(),env:process.env,
  shell:false,windowsHide:true,stdio:['pipe','pipe','pipe']})
process.stdin.pipe(child.stdin);child.stdout.pipe(process.stdout);child.stderr.pipe(process.stderr)
child.stdin.on('error',e=>{if(e.code!=='EPIPE')process.exitCode=1})
child.on('error',e=>{process.stderr.write('CLI_SPAWN_'+(e.code||'ERROR')+'\n');process.exitCode=1})
child.on('close',code=>{process.exitCode=code??1})
