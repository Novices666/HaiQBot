# run-with-env.ps1 —— 用环境变量 / .env 里的口令起 haiqbot（给 dev-up.ps1 调用，也可单独跑）
#
# 为什么不直接把口令塞进命令行：
#   WMI Win32_Process 创建的进程不继承调用者的环境变量，只能靠「命令行」或「脚本自己读」。
#   让脚本自己读 .env，命令行上就只剩路径，口令不会出现在进程列表 / 日志 / 历史记录里。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\run-with-env.ps1
#   ... -Config haiq.toml -Log logs\haiq.out        # 可覆盖
#   ... -Foreground                                 # 日志直接打屏（调试用）

param(
    [string]$Config = "haiq.toml",
    [string]$Log = "logs\haiq.out",
    [switch]$Foreground
)

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "envpass.ps1")

$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "x 找不到 $py —— 先跑一次 uv sync --extra dev" -ForegroundColor Red
    exit 1
}

$pass = Get-VaultPass -Root $root
if (-not $pass) {
    Write-VaultPassHint
    exit 1
}

$env:HAIQ_VAULT_PASSPHRASE = $pass
$pass = $null

Push-Location $root
try {
    # 原生命令的 stderr 不该因为 EAP=Stop 变成终止异常
    $ErrorActionPreference = "Continue"
    if ($Foreground) {
        & $py -m haiqbot --config $Config run
    }
    else {
        $logPath = Join-Path $root $Log
        $logDir = Split-Path -Parent $logPath
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
        # 用 cmd 的 >> 而不是 PowerShell 的 *>>：后者会用 UTF-16LE 重写日志。
        # cmd 继承本进程环境（口令已在 $env 里），所以命令行上依然没有口令。
        $inner = "`"$py`" -m haiqbot --config $Config run >> `"$logPath`" 2>&1"
        & cmd.exe /c $inner
    }
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
    $env:HAIQ_VAULT_PASSPHRASE = $null
}

exit $code
