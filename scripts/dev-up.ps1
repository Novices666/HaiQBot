# dev-up.ps1 —— 本机开发一键起链路：协议端容器（可选）-> haiqbot
#
# **这是作者本机的开发脚本，不是框架的一部分。**
#   - 协议端（NapCat / Lagrange / …）不属于本框架。本脚本只会 `docker start` 一个
#     你已经存在的容器；没有就跳过。它不读本仓库的 compose.yaml，也不替你创建协议端。
#   - 它真正做的事只有一件：把 haiqbot 拉起来。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1
#   ... -SkipProtocol                                  # 协议端我自己管
#   ... -SkipBot                                       # 只动协议端
#   ... -ProtocolContainer napcat -ProtocolPort 3001
#
# 口令处理：口令只从环境变量 HAIQ_VAULT_PASSPHRASE 来；本机放在项目根 .env 里（不入库）。
# 起服务由 scripts\run-with-env.ps1 负责（它自己读 .env），命令行里只有脚本路径，口令不打印。
# 子进程用 WMI Win32_Process 创建，因此关掉当前 PowerShell 窗口不会把它带走。

param(
    [Alias("SkipDocker")][switch]$SkipProtocol,
    [switch]$SkipBot,
    [string]$ProtocolContainer = "napcat",
    [int]$ProtocolPort = 3001,
    [int]$ProtocolWaitSeconds = 120,
    [int]$DockerWaitSeconds = 180,
    [int]$WebuiWaitSeconds = 90
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$runner = Join-Path $PSScriptRoot "run-with-env.ps1"
. (Join-Path $PSScriptRoot "envpass.ps1")
$logFile = Join-Path $root "logs\haiq.out"
$cfg = Join-Path $root "haiq.toml"

function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "   ok $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   !  $msg" -ForegroundColor Yellow }
function Die($msg) {
    Write-Host "   x  $msg" -ForegroundColor Red
    exit 1
}

function Test-Port([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 1500) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect($TargetHost, $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false) -and $c.Connected) {
            $c.EndConnect($iar)
            return $true
        }
        return $false
    }
    catch { return $false }
    finally { $c.Close() }
}

function Test-Http([string]$Url, [int]$TimeoutSec = 3) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400)
    }
    catch { return $false }
}

# --------------------------------------------------------------------------- #
# 0. 前置检查
# --------------------------------------------------------------------------- #

Step "前置检查"
if (-not (Test-Path $py)) { Die "找不到 $py —— 先在项目根目录跑 uv sync" }
Ok "python: $py"
if (-not (Test-Path $cfg)) { Die "找不到 $cfg" }
Ok "配置: $cfg"

# 端口：从 haiq.toml 的 [webui] port 读，读不到就用 8080
$webuiPort = 8080
$m = Select-String -Path $cfg -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
if ($m) { $webuiPort = [int]$m.Matches[0].Groups[1].Value }
Ok "WebUI 端口: $webuiPort"

# --------------------------------------------------------------------------- #
# 1. Docker Desktop
# --------------------------------------------------------------------------- #

if (-not $SkipProtocol) {
    Step "协议端容器（可选）"
    # 协议端不归本框架管：这里最多帮你 start 一个已存在的容器。
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    docker info --format '{{.ServerVersion}}' *>$null
    $dockerUp = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eap

    if (-not $dockerUp) {
        $candidates = @(
            "D:\Docker\Docker Desktop.exe",
            "C:\Program Files\Docker\Docker\Docker Desktop.exe",
            (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
        )
        $exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($exe) {
            Warn "启动 Docker Desktop: $exe"
            Start-Process -FilePath $exe | Out-Null
            $deadline = (Get-Date).AddSeconds($DockerWaitSeconds)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 3
                $ErrorActionPreference = "Continue"
                docker info --format '{{.ServerVersion}}' *>$null
                if ($LASTEXITCODE -eq 0) { $dockerUp = $true; break }
                Write-Host "." -NoNewline
            }
            $ErrorActionPreference = $eap
            Write-Host ""
        }
    }

    if (-not $dockerUp) {
        Warn "Docker 没在运行 —— 跳过协议端；需要容器就自己启 Docker Desktop"
    }
    else {
        $ErrorActionPreference = "Continue"
        docker inspect $ProtocolContainer *>$null
        $exists = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $eap

        if ($exists) {
            docker start $ProtocolContainer | Out-Null
            Ok "已启动容器 $ProtocolContainer（复用，不重建）"

            $deadline = (Get-Date).AddSeconds($ProtocolWaitSeconds)
            $up = $false
            while ((Get-Date) -lt $deadline) {
                if (Test-Port "127.0.0.1" $ProtocolPort) { $up = $true; break }
                Start-Sleep -Seconds 2
                Write-Host "." -NoNewline
            }
            Write-Host ""
            if ($up) { Ok "协议端 $ProtocolPort 已监听" }
            else { Warn "$ProtocolPort 还没监听 —— 可能没登录，看协议端自己的文档" }
        }
        else {
            Warn "没找到容器 $ProtocolContainer —— 协议端请自己起，本脚本不管它"
        }
    }
}
else {
    Step "跳过协议端"
}

# --------------------------------------------------------------------------- #
# 3. haiqbot
# --------------------------------------------------------------------------- #

if (-not $SkipBot) {
    Step "haiqbot"
    if (Test-Http "http://127.0.0.1:$webuiPort/") {
        Ok "已经在跑（WebUI $webuiPort 有响应），跳过启动"
    }
    else {
        if (-not (Get-VaultPass -Root $root)) {
            Write-VaultPassHint
            exit 1
        }
        if (-not (Test-Path (Split-Path -Parent $logFile))) { New-Item -ItemType Directory -Path (Split-Path -Parent $logFile) | Out-Null }

        # 口令不进命令行：run-with-env.ps1 自己从环境变量 / .env 读，设进自己的进程环境，再起 python
        $cmd = "cmd.exe /c powershell -NoProfile -ExecutionPolicy Bypass -File `"$runner`""
        $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
        if ($r.ReturnValue -ne 0) { Die "进程创建失败（ReturnValue=$($r.ReturnValue)）" }
        Ok "已拉起 pid=$($r.ProcessId)（分离进程，关窗口不影响）"

        $deadline = (Get-Date).AddSeconds($WebuiWaitSeconds)
        $ok = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            if (Test-Http "http://127.0.0.1:$webuiPort/") { $ok = $true; break }
            Write-Host "." -NoNewline
        }
        Write-Host ""
        if ($ok) { Ok "WebUI 已就绪" }
        else {
            Warn "WebUI 没起来，看日志尾部："
            if (Test-Path $logFile) { Get-Content $logFile -Tail 20 | ForEach-Object { Write-Host "     $_" } }
            exit 1
        }
    }
}
else {
    Step "跳过 haiqbot"
}

# --------------------------------------------------------------------------- #
# 4. 汇总
# --------------------------------------------------------------------------- #

Step "就绪"
Write-Host "   WebUI       http://127.0.0.1:$webuiPort/"
Write-Host "   协议端      自己管（本脚本只在检测到容器 $ProtocolContainer 时 start 它）"
Write-Host "   日志        $logFile"
Write-Host "   追日志      powershell -ExecutionPolicy Bypass -File scripts\logs.ps1   （另开终端，黑框里没有输出）"
Write-Host ""
Write-Host "   停止 haiqbot：在任务管理器结束 python.exe，或 haiq run 前台 Ctrl+C"
