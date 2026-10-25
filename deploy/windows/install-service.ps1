# 把 haiqbot 注册成 Windows 服务（需要 NSSM：https://nssm.cc/ 或 winget install NSSM）
#
#   powershell -ExecutionPolicy Bypass -File .\deploy\windows\install-service.ps1
#   powershell -ExecutionPolicy Bypass -File .\deploy\windows\install-service.ps1 -Uninstall
#
# 参数默认取脚本所在仓库根目录；可用 -RepoRoot 覆盖。

param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path,
    [string]$ServiceName = "haiqbot",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Find-Nssm {
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @("$env:ProgramFiles\nssm\nssm.exe", "${env:ProgramFiles(x86)}\nssm\nssm.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$nssm = Find-Nssm
if (-not $nssm) {
    Write-Error "找不到 nssm.exe。请先安装 NSSM（winget install NSSM.NSSM）后重试。"
}

if ($Uninstall) {
    & $nssm stop $ServiceName 2>$null
    & $nssm remove $ServiceName confirm
    Write-Host "已移除服务 $ServiceName"
    exit 0
}

$venvPython = Join-Path $RepoRoot ".venv\Scripts\haiq.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "找不到 $venvPython。请先在仓库根目录执行：uv sync --extra official --extra dev"
}

$logDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "注册服务 $ServiceName -> $venvPython"
& $nssm install $ServiceName $venvPython "run"
& $nssm set $ServiceName AppDirectory $RepoRoot
& $nssm set $ServiceName AppStdout (Join-Path $logDir "haiqbot.out.log")
& $nssm set $ServiceName AppStderr (Join-Path $logDir "haiqbot.err.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 10485760
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName Description "haiqbot —— 协议无关的 QQ Bot 框架"

# 服务读不到 .env：vault 主口令必须落进服务自己的环境变量，否则服务一启动就说「凭据库打不开」。
# 口令从当前环境变量 / 仓库根 .env 取（scripts\envpass.ps1）；找不到就提醒，不硬闯。
. (Join-Path $RepoRoot "scripts\envpass.ps1")
$vaultPass = Get-VaultPass -Root $RepoRoot
if ($vaultPass) {
    & $nssm set $ServiceName AppEnvironmentExtra "HAIQ_VAULT_PASSPHRASE=$vaultPass" | Out-Null
    $vaultPass = $null
    Write-Host "ok 已把 HAIQ_VAULT_PASSPHRASE 写进服务环境变量（存在注册表里，别在共用机器上这么干）" -ForegroundColor Green
}
else {
    Write-Host "! 没拿到 vault 主口令，服务启动后会打不开凭据库" -ForegroundColor Yellow
    Write-Host "  补上：nssm set $ServiceName AppEnvironmentExtra HAIQ_VAULT_PASSPHRASE=<口令>" -ForegroundColor Yellow
}

Write-Host "启动服务…"
& $nssm start $ServiceName
Start-Sleep -Seconds 2
& $nssm status $ServiceName
Write-Host ""
Write-Host "完成。查看日志：Get-Content '$logDir\haiqbot.out.log' -Tail 50 -Wait"
