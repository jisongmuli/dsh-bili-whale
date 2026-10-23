param([string]$BiliHome='',[string]$BaseUrl='',[string]$SessionId='',[string]$Token='')
$ErrorActionPreference='Stop'
try{$Host.UI.RawUI.WindowTitle='大肥鱼 - 切换Google账号'}catch{}
if(-not $BiliHome){$BiliHome=Join-Path $(if($env:DSH_HOME){$env:DSH_HOME}else{Join-Path $env:USERPROFILE '.dsh'}) 'bili-duty'}
$done=$false
function Read-Json($path){Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json}
function Request($operation,$extra=@{}){
    $body=@{operation=$operation;id=$SessionId;token=$Token}
    foreach($key in $extra.Keys){$body[$key]=$extra[$key]}
    Invoke-RestMethod -Uri ($BaseUrl+'/api/bili-duty/model-account') -Method Post -ContentType 'application/json' -Body ($body|ConvertTo-Json -Depth 8) -TimeoutSec 30
}
try{
    if(-not $BaseUrl){
        $runtime=Read-Json (Join-Path $BiliHome 'runtime-status.json')
        $listener=Get-NetTCPConnection -State Listen -OwningProcess $runtime.runtimePid | Where-Object {$_.LocalAddress -eq '127.0.0.1'} | Select-Object -First 1
        if(-not $listener){throw '请先启动 DeepSeek Harness，再切换模型账号'}
        $BaseUrl='http://127.0.0.1:'+$listener.LocalPort
    }
    if($BaseUrl -notmatch '^http://127\.0\.0\.1:\d+$'){throw '只允许连接本机大肥鱼后台'}
    if(-not $SessionId){
        $session=Request 'begin'
        if(-not $session.started){throw '已有账号切换窗口，请先完成或关闭那个窗口'}
        $SessionId=$session.id;$Token=$session.token
    }
    Write-Host '正在等待大肥鱼当前任务结束；消息会继续收取并保留。'
    $until=(Get-Date).AddMinutes(35)
    while(-not (Request 'ready').ready){
        if((Get-Date) -gt $until){throw '任务仍在执行，暂未切换；可以稍后重试'}
        Start-Sleep -Seconds 2
    }
    $node=Get-Command node.exe -ErrorAction SilentlyContinue
    if($node){$nodePath=$node.Source}else{
        $current=Get-CimInstance Win32_Process -Filter ('ProcessId='+(Read-Json (Join-Path $BiliHome 'runtime-status.json')).runtimePid)
        $nodePath=Join-Path (Split-Path -Parent $current.ExecutablePath) 'resources/runtime/primary-runtime/dependencies/node/bin/node.exe'
    }
    $loginConfig=& $nodePath (Join-Path $PSScriptRoot '../lib/model-account-switch.mjs') --login-config $BiliHome | ConvertFrom-Json
    if($LASTEXITCODE -ne 0 -or -not $loginConfig.binary){throw '找不到可用的官方 Antigravity CLI'}
    $binary=$loginConfig.binary
    foreach($name in @('GEMINI_API_KEY','GOOGLE_API_KEY','AGY_ADC_AUTH','GOOGLE_GENAI_USE_VERTEXAI','GOOGLE_GEMINI_BASE_URL','NODE_USE_ENV_PROXY')){
        [Environment]::SetEnvironmentVariable($name,$null,'Process')
    }
    foreach($property in $loginConfig.env.PSObject.Properties){
        [Environment]::SetEnvironmentVariable($property.Name,[string]$property.Value,'Process')
    }
    $settingsPath=Join-Path $env:USERPROFILE '.gemini/antigravity-cli/settings.json'
    [IO.Directory]::CreateDirectory((Split-Path -Parent $settingsPath)) | Out-Null
    $settings=if(Test-Path -LiteralPath $settingsPath){Read-Json $settingsPath}else{[pscustomobject]@{}}
    if($settings.modelProvider -eq 'gemini'){throw '当前CLI是API密钥模式，请先恢复官方Google账号登录'}
    $settings | Add-Member -NotePropertyName useG1Credits -NotePropertyValue $false -Force
    [IO.File]::WriteAllText($settingsPath,($settings|ConvertTo-Json -Depth 30),(New-Object Text.UTF8Encoding($false)))
    Write-Host ''
    Write-Host '接下来进入官方 CLI：'
    Write-Host '1. 若仍是旧账号，输入 /logout 并按回车。'
    Write-Host '2. 在官方浏览器登录页面选择另一个已登录的 Google 账号。'
    Write-Host '3. 核对 CLI 顶部账号，再输入 /exit 返回。必要时退出后会重新打开登录窗口。'
    Write-Host '本机 Antigravity App、CLI 共用登录。本次只切换Google登录，B站身份与大肥鱼记忆保留。'
    Read-Host '准备好后按回车打开官方 CLI' | Out-Null
    do{
        & $binary
        if($LASTEXITCODE -ne 0){throw '官方 CLI 异常退出，登录未完成；不会将旧登录记为切换成功。请保留报错后重试。'}
        $answer=Read-Host '若已登录目标账号，按回车验证；输入 R 重新打开登录，输入 Q 取消'
    }while($answer -eq 'R')
    if($answer -eq 'Q'){return}
    $settings=Read-Json $settingsPath
    $settings | Add-Member -NotePropertyName useG1Credits -NotePropertyValue $false -Force
    [IO.File]::WriteAllText($settingsPath,($settings|ConvertTo-Json -Depth 30),(New-Object Text.UTF8Encoding($false)))
    $verification=& $nodePath (Join-Path $PSScriptRoot '../lib/model-account-switch.mjs') --verify $BiliHome | ConvertFrom-Json
    if(-not $verification.success){throw ('登录验证未通过：'+$verification.code)}
    Request 'finish' @{verification=$verification} | Out-Null
    $done=$true
    Write-Host '当前Google账号已通过Gemini调用验证，大肥鱼已恢复任务。'
}catch{
    Write-Host ('未完成切换：'+$_.Exception.Message)
}finally{
    if(-not $done -and $SessionId -and $Token){try{Request 'cancel' | Out-Null}catch{}}
    Read-Host '按回车关闭窗口' | Out-Null
}
