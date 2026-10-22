param([string]$DshHome)
$ErrorActionPreference='Stop'
if(-not $DshHome){$DshHome=if($env:DSH_HOME){$env:DSH_HOME}else{Join-Path $env:USERPROFILE '.dsh'}}
$botHome=Join-Path $DshHome 'bili-duty'
$failed=0
function Check([string]$Label,[bool]$Passed){if($Passed){Write-Host ('[OK] '+$Label)}else{Write-Host ('[待处理] '+$Label);$script:failed++}}
function ReadJson([string]$Name){$path=Join-Path $botHome $Name;if(Test-Path -LiteralPath $path){return Get-Content -LiteralPath $path -Raw|ConvertFrom-Json};return $null}
$identity=ReadJson 'identity.json';$install=ReadJson 'installation.json';$routing=ReadJson 'model-routing.json'
Check '两个不同账号已绑定' ($identity.ownerUid -match '^[1-9][0-9]*$' -and $identity.botUid -match '^[1-9][0-9]*$' -and $identity.ownerUid -ne $identity.botUid)
Check '宿主程序与可移植启动器' ($install -and (Test-Path -LiteralPath $install.appPath) -and (Test-Path -LiteralPath (Join-Path $botHome 'desktop-dsh.cjs')))
Check 'Antigravity CLI' ($routing -and (Test-Path -LiteralPath $routing.antigravityCli))
$py=Join-Path $botHome '.venv/Scripts/python.exe'
Check '独立Python环境' (Test-Path -LiteralPath $py)
if(Test-Path -LiteralPath $py){
 & $py -c 'import bilibili_api,mcp,httpx,PIL,playwright,imageio_ffmpeg'
 Check 'Python依赖可导入' ($LASTEXITCODE -eq 0)
}
$bot=ReadJson 'bili_credential.json';$history=ReadJson 'owner-history-credential.json'
Check '机器人账号扫码登录' ($bot -and $bot.dedeuserid -eq $identity.botUid -and [bool]$bot.sessdata)
Check '主账号历史授权' ($history -and $history.dedeuserid -eq $identity.ownerUid -and [bool]$history.sessdata)
$settings=Join-Path $env:USERPROFILE '.gemini/antigravity-cli/settings.json'
$settingsValue=if(Test-Path -LiteralPath $settings){Get-Content -LiteralPath $settings -Raw|ConvertFrom-Json}else{$null}
Check 'CLI超额积分保持关闭' (-not $settingsValue -or $settingsValue.useG1Credits -in @($null,$false,'off'))
Write-Host '登录文件存在不代表远端会话仍有效；最后请在设置页点“测一下”，再做一次私信或看视频测试。'
Write-Host '未读取或打印Cookie、令牌、密钥和聊天内容。'
exit ([int]($failed -gt 0))
