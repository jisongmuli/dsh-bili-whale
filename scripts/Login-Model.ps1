$ErrorActionPreference='Stop'
$agy=Join-Path $env:LOCALAPPDATA 'agy/bin/agy.exe'
if(-not(Test-Path -LiteralPath $agy)){
 $found=Get-Command agy.exe -ErrorAction SilentlyContinue
 if(-not $found){throw '请先运行安装.cmd安装官方CLI'}
 $agy=$found.Source
}
Write-Host '请在官方CLI中登录自己的Google账号，保持超额积分关闭。完成后退出CLI。'
& $agy
