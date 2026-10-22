# 凭据迁移一键脚本（vault.bin → vault.json，aead 加密）
#
# 用法：在项目根目录执行
#   powershell -ExecutionPolicy Bypass -File .\scripts\migrate-vault.ps1
#
# 密码不会落盘、不会进 shell history：
#   - 第 1 步 reencrypt 用 getpass 读两次（确认）
#   - 第 2 步为本次验证临时放进进程环境变量，脚本结束即失效

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$haiqbot = Join-Path $root '.venv\Scripts\haiq.exe'
if (-not (Test-Path $haiqbot)) {
    Write-Host "找不到 $haiqbot —— 先在项目根跑 uv sync" -ForegroundColor Red
    exit 1
}

Write-Host '=== 1/3 迁移凭据 ===' -ForegroundColor Cyan
Write-Host '（会让你输两次密码，不回显；忘记密码 = 凭据全丢，建议同时导出一份）'
& $haiq secrets reencrypt
if ($LASTEXITCODE -ne 0) {
    Write-Host "迁移未完成（退出码 $LASTEXITCODE），已中止。" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host '=== 2/3 输入刚才的密码（仅用于本次验证，不落盘）===' -ForegroundColor Cyan
$secure = Read-Host -AsSecureString 'vault 密码'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:WEFT_VAULT_PASSPHRASE = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

Write-Host ''
Write-Host '=== 3/3 验证 ===' -ForegroundColor Cyan
& $haiq secrets list
Write-Host ''
& $haiq doctor
$doctorCode = $LASTEXITCODE

Write-Host ''
switch ($doctorCode) {
    0 { Write-Host 'doctor：全部通过' -ForegroundColor Green }
    1 { Write-Host 'doctor：有警告（不影响运行）' -ForegroundColor Yellow }
    default { Write-Host "doctor：有错误（退出码 $doctorCode）" -ForegroundColor Red }
}

Write-Host ''
Write-Host '别忘了把这个密码配置到服务环境（否则下次启动读不到凭据）：' -ForegroundColor Yellow
Write-Host '  Windows NSSM : nssm set haiqbot AppEnvironmentExtra WEFT_VAULT_PASSPHRASE=<密码>'
Write-Host '  Linux systemd: /etc/haiqbot/env 写 WEFT_VAULT_PASSPHRASE=<密码>，chmod 600'
Write-Host '  本次会话临时: $env:WEFT_VAULT_PASSPHRASE = "<密码>"'

$env:WEFT_VAULT_PASSPHRASE = $null
