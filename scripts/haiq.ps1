# haiq.ps1 —— 带上 vault 口令跑 haiq：口令取自环境变量或本机 .env，只在本进程环境里放一次
#
# 为什么需要它：
#   口令只从 HAIQ_VAULT_PASSPHRASE 读（内核不读 .env）。没有这个包装，每开一个终端都要
#   手敲一遍 `$env:HAIQ_VAULT_PASSPHRASE=...`；有了它，口令只需要待在 .env 里。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 doctor
#   ... scripts\haiq.ps1 webui token --generate
#   ... scripts\haiq.ps1 channels verify
#
# 口令不打印、不进命令行：命令行里只有脚本路径和 haiq 自己的参数。

param(
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Args
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "envpass.ps1")

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "x 找不到 $py —— 先在项目根目录跑一次 uv sync --extra dev" -ForegroundColor Red
    exit 1
}
if (-not $Args -or $Args.Count -eq 0) {
    Write-Host "用法：scripts\haiq.ps1 <子命令> [参数...]   例如：scripts\haiq.ps1 doctor" -ForegroundColor Yellow
    exit 1
}

$pass = Get-VaultPass -Root $root
if ($pass) {
    $env:HAIQ_VAULT_PASSPHRASE = $pass
    $pass = $null
}
else {
    # 不拦：--help / init / --version 这类命令用不着口令，缺了就让它自己报（它会说人话）
    Write-Host "! 没有 vault 主口令（环境变量或 .env 里的 HAIQ_VAULT_PASSPHRASE）" -ForegroundColor Yellow
}

Push-Location $root
try {
    # 原生命令的 stderr 不该因为 EAP=Stop 变成终止异常
    $ErrorActionPreference = "Continue"
    & $py -m haiqbot @Args
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
    $env:HAIQ_VAULT_PASSPHRASE = $null
}

exit $code
