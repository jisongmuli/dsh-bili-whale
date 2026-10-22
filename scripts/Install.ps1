[CmdletBinding()]
param(
 [string]$OwnerUid,[string]$BotUid,[string]$OwnerName='主人',[string]$BotName='大肥鱼',
 [string]$DshHome,[string]$AppPath,[string]$Model='gemini-3.8-flash-low',
 [switch]$SkipCliInstall,[switch]$SkipDependencies,[switch]$PrepareOnly
)
$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if(-not $DshHome){$DshHome=if($env:DSH_HOME){$env:DSH_HOME}else{Join-Path $env:USERPROFILE '.dsh'}}
$DshHome=[IO.Path]::GetFullPath($DshHome)
$botHome=Join-Path $DshHome 'bili-duty'
if(-not $OwnerUid){$OwnerUid=Read-Host '你的主账号B站UID（纯数字）'}
if(-not $BotUid){$BotUid=Read-Host '给鲸鱼娘使用的B站小号UID（不要填主账号）'}
if($OwnerUid -notmatch '^[1-9][0-9]*$' -or $BotUid -notmatch '^[1-9][0-9]*$' -or $OwnerUid -eq $BotUid){throw '请填写两个不同的真实UID'}
if(-not $AppPath){
 $running=Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue | Select-Object -First 1
 if($running){$AppPath=$running.Path}
 if(-not $AppPath){
  foreach($root in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*')){
   foreach($app in (Get-ItemProperty $root -ErrorAction SilentlyContinue | Where-Object {$_.DisplayName -like '*DeepSeek*Harness*'})){
    if(-not $app.InstallLocation){continue}
    $guess=Join-Path ([string]$app.InstallLocation) 'DeepSeek Harness.exe'
    if(Test-Path -LiteralPath $guess){$AppPath=$guess;break}
   }
   if($AppPath){break}
  }
 }
 if(-not $AppPath){
  Add-Type -AssemblyName System.Windows.Forms
  $picker=New-Object System.Windows.Forms.OpenFileDialog
  $picker.Title='请选择已安装的 DeepSeek Harness.exe';$picker.Filter='DeepSeek Harness|DeepSeek Harness.exe'
  if($picker.ShowDialog() -ne 'OK'){throw '没有选择宿主程序，安装尚未开始'}
  $AppPath=$picker.FileName
 }
}
$AppPath=[IO.Path]::GetFullPath($AppPath)
if(-not(Test-Path -LiteralPath $AppPath)){throw '宿主程序不存在'}
$appDir=Split-Path -Parent $AppPath
$node=Join-Path $appDir 'resources/runtime/primary-runtime/dependencies/node/bin/node.exe'
$cli=Join-Path $appDir 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
if(-not(Test-Path -LiteralPath $node)){throw '未找到宿主自带Node。请更新DeepSeek Harness，或使用文档中的手动安装方式'}
if(-not $PrepareOnly){
 $open=@(Get-Process -ErrorAction SilentlyContinue | Where-Object {$_.Path -eq $AppPath})
 if($open.Count -gt 0){throw '请先从托盘完全退出DeepSeek Harness，再重新运行安装。不强制中断正在执行的任务'}
}
$agy=Join-Path $env:LOCALAPPDATA 'agy/bin/agy.exe'
if(-not(Test-Path -LiteralPath $agy)){
 $found=Get-Command agy.exe -ErrorAction SilentlyContinue
 if($found){$agy=$found.Source}
}
if(-not(Test-Path -LiteralPath $agy) -and -not $SkipCliInstall){
 Write-Host '从Google官方地址安装Antigravity CLI...'
 $officialInstaller=Join-Path ([IO.Path]::GetTempPath()) ('whale-agy-'+[guid]::NewGuid().ToString('N')+'.ps1')
 Invoke-WebRequest -UseBasicParsing -Uri 'https://antigravity.google/cli/install.ps1' -OutFile $officialInstaller
 & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $officialInstaller
 if($LASTEXITCODE -ne 0){throw '官方CLI安装失败；请按照README完成CLI安装后重试'}
 $agy=Join-Path $env:LOCALAPPDATA 'agy/bin/agy.exe'
}
if(-not(Test-Path -LiteralPath $agy) -and -not $PrepareOnly){throw 'CLI未安装，不能完成自动执行环境'}
New-Item -ItemType Directory -Path (Join-Path $botHome 'tmp') -Force | Out-Null
$options=@{repo=$repo;home=$botHome;ownerUid=$OwnerUid;botUid=$BotUid;ownerName=$OwnerName;botName=$BotName;appPath=$AppPath;cliPath=$cli;nodePath=$node;agyPath=$agy;model=$Model}
$request=Join-Path $botHome 'tmp/install-options.json'
$utf8=New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($request,($options|ConvertTo-Json),$utf8)
& $node (Join-Path $repo 'scripts/configure.mjs') $request
if($LASTEXITCODE -ne 0){throw '配置失败，未执行插件注册'}
if($PrepareOnly){Write-Host '只完成隔离目录准备，未注册插件、安装依赖或启动任何服务';exit 0}
$previousDshHome=$env:DSH_HOME;$previousElectron=$env:ELECTRON_RUN_AS_NODE
try{
 $env:DSH_HOME=$DshHome;$env:ELECTRON_RUN_AS_NODE='1'
 & $AppPath --expose-internals --require (Join-Path $repo 'lib/model-preload.cjs') (Join-Path $repo 'scripts/host-cli.cjs') $cli plugin --profile desktop add ('file:'+$repo) | Out-Host
 if($LASTEXITCODE -ne 0){throw '插件注册失败，请查看上方日志'}
}finally{$env:DSH_HOME=$previousDshHome;$env:ELECTRON_RUN_AS_NODE=$previousElectron}
if(-not $SkipDependencies){
 & $node (Join-Path $repo 'scripts/dependencies.mjs') $botHome
 if($LASTEXITCODE -ne 0){throw 'Python依赖尚未完成，可在设置→刷B站→装主依赖重试'}
}
Write-Host ''
Write-Host '安装完成。接下来：'
Write-Host '1. 运行 agy 完成你自己的Google账号登录，关闭超额积分使用。'
Write-Host '2. 启动DeepSeek Harness，设置→刷B站，用机器人小号扫码登录。'
Write-Host '3. 再扫“主账号历史登录”，使用你自己的主账号。'
Write-Host '4. 按README打开评论/私信回复、主人任务与自动浏览。'
Write-Host ('数据仅保存在：'+$botHome)
