'use strict';
// Electron ASAR modules may not expose import.meta.main. Invoke the exported
// desktop CLI explicitly rather than accepting a silent successful no-op.
const {join,dirname}=require('node:path');
const {pathToFileURL}=require('node:url');
const cli=process.argv[2];
process.argv=[process.argv[0],process.argv[1],...process.argv.slice(3)];
const appDir=dirname(process.execPath);
import(pathToFileURL(cli).href)
 .then(({runDesktopCli})=>runDesktopCli(join(appDir,'resources','app.asar','dsh'),join(appDir,'resources','runtime')))
 .catch(error=>{console.error(error.message);process.exitCode=1});
