# dev-up.ps1 —— 本地开发一键起链路：Docker Desktop -> napcat 容器 -> haiqbot
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1 -SkipDocker   # 容器已在跑
#   powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1 -SkipBot      # 只起容器
#
# 口令处理：口令只从环境变量 HAIQ_VAULT_PASSPHRASE 来；本机放在项目根 .env 里（不入库）。
# 起服务由 scripts\run-with-env.ps1 负责（它自己读 .env），命令行里只有脚本路径，口令不打印。
# 子进程用 WMI Win32_Process 创建，因此关掉当前 PowerShell 窗口不会把它带走。

param(
    [switch]$SkipDocker,
    [switch]$SkipBot,
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

if (-not $SkipDocker) {
    Step "Docker 守护进程"
    # 同样要临时放宽 EAP：守护进程没起来时 docker 会往 stderr 写
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    docker info --format '{{.ServerVersion}}' *>$null
    $running = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eap

    if ($running) {
        Ok "已在运行"
    }
    else {
        $candidates = @(
            "D:\Docker\Docker Desktop.exe",
            "C:\Program Files\Docker\Docker\Docker Desktop.exe",
            (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
        )
        $exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $exe) { Die "Docker 没在跑，且没找到 Docker Desktop.exe（改一遍脚本里的 candidates）" }
        Warn "启动 Docker Desktop: $exe"
        Start-Process -FilePath $exe | Out-Null

        $deadline = (Get-Date).AddSeconds($DockerWaitSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
            try { docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $running = $true; break } } catch { }
            Write-Host "." -NoNewline
        }
        Write-Host ""
        if (-not $running) { Die "等了 $DockerWaitSeconds 秒 Docker 还是没起来" }
        Ok "守护进程就绪"
    }

    # ----------------------------------------------------------------------- #
    # 2. napcat 容器
    # ----------------------------------------------------------------------- #
    Step "napcat 容器（compose profile: onebot）"
    Push-Location $root
    try {
        # 有的机器只装了独立版 docker-compose，没有 compose 插件 —— 两种都试。
        # 探测失败时 docker 会往 stderr 写，而 $ErrorActionPreference=Stop 会把它当异常，
        # 所以探测期间临时放宽，改看 $LASTEXITCODE。
        $eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        docker compose version *>$null
        $hasPlugin = ($LASTEXITCODE -eq 0)
        $hasStandalone = $false
        if (-not $hasPlugin) {
            docker-compose version *>$null
            $hasStandalone = ($LASTEXITCODE -eq 0)
        }
        $ErrorActionPreference = $eap

        $composeExe = $null
        $composePrefix = @()
        if ($hasPlugin) { $composeExe = "docker"; $composePrefix = @("compose") }
        elseif ($hasStandalone) { $composeExe = "docker-compose" }
        else { Die "既没有 docker compose（插件）也没有 docker-compose（独立版）" }
        Ok "compose: $composeExe $($composePrefix -join ' ')"

        # compose.yaml 里 haiqbot 服务声明了 env_file: .env —— 即使只起 onebot profile，
        # compose 也会校验所有服务，缺 .env 直接报错。示例文件里全是空值，可以安全复制。
        $envFile = Join-Path $root ".env"
        if (-not (Test-Path $envFile)) {
            Copy-Item (Join-Path $root ".env.example") $envFile
            Warn "没有 .env，已从 .env.example 复制一份空模板（不含任何密钥）"
        }

        # 只要 napcat：dev 流程里 haiqbot 跑在 venv 里，不需要它的镜像，
        # 也就不该顺带触发 haiqbot 镜像构建（要拉 python 基础镜像，断网就卡死）。
        #
        # 若已存在同名容器（例如历史上手工 docker run 起的、非 compose 管理），
        # 直接 start 复用：重建会撞 container_name，也没必要丢掉正在用的登录态。
        $eap2 = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        docker inspect napcat *>$null
        $exists = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $eap2

        if ($exists) {
            Ok "已有容器 napcat，直接复用（不重建）"
            # 用 docker start 而不是 compose start：后者只认本项目 compose 管理的容器
            docker start napcat | Out-Null
        }
        else {
            $composeArgs = @()
            $composeArgs += $composePrefix
            $composeArgs += @("--profile", "onebot", "up", "-d", "napcat")
            & $composeExe @composeArgs
            if ($LASTEXITCODE -ne 0) { Die "compose up 失败（退出码 $LASTEXITCODE）" }
        }
    }
    finally { Pop-Location }
    Ok "容器已启动"

    $deadline = (Get-Date).AddSeconds(120)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-Port "127.0.0.1" 3001) { $up = $true; break }
        Start-Sleep -Seconds 2
        Write-Host "." -NoNewline
    }
    Write-Host ""
    if ($up) { Ok "OneBot WS 3001 已监听" }
    else { Warn "3001 还没监听 —— QQ 可能没登录：打开 http://127.0.0.1:6099 扫码" }
}
else {
    Step "跳过 Docker"
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
Write-Host "   NapCat 面板 http://127.0.0.1:6099/   （扫码登录 / 改配置）"
Write-Host "   OneBot WS   ws://127.0.0.1:3001"
Write-Host "   日志        $logFile"
Write-Host ""
Write-Host "   停止容器：docker compose --profile onebot down（或 docker-compose --profile onebot down）"
Write-Host "   停止 haiqbot    ：在任务管理器结束 python.exe，或 haiq run 前台 Ctrl+C"
