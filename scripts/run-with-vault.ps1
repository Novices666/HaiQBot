# run-with-vault.ps1 —— 用 DPAPI 文件里的口令起 haiqbot（给 dev-up.ps1 调用，也可单独跑）
#
# 为什么单独一个脚本：
#   WMI Win32_Process 创建的进程不继承调用者的环境变量，所以不能在 dev-up.ps1
#   里把口令塞进环境再 Create。改由本脚本自己解口令 -> 设进自己的环境 -> 起 python，
#   命令行上只出现路径，不出现口令。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\run-with-vault.ps1
#   ... -Config haiq.toml -Log logs\haiq.out      # 可覆盖

param(
    [string]$Config = "haiq.toml",
    [string]$Log = "logs\haiq.out",
    # 前台跑（调试用）：日志直接打屏，不写文件
    [switch]$Foreground
)

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "vaultpass.ps1")

$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "x 找不到 $py —— 先跑一次 uv sync" -ForegroundColor Red
    exit 1
}

$pass = Read-VaultPass -Root $root
if (-not $pass) {
    Write-Host "x 找不到口令：secrets\.vault-pass.dpapi（或旧的 .vault-pass.txt）" -ForegroundColor Red
    Write-Host "  生成：powershell -ExecutionPolicy Bypass -File scripts\rotate-vault-pass.ps1" -ForegroundColor Yellow
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
