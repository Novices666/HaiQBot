# 轮换 haiq vault 口令，并清掉明文密码文件。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\rotate-vault-pass.ps1
#
# 这个脚本不替你决定口令，也不会把口令写进任何日志：
#   - 旧口令：优先从 secrets\.vault-pass.txt 读取（只进环境变量，不打印）
#   - 新口令：由 haiq secrets passwd 交互输入（两次，不回显）

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$haiqbot = Join-Path $root ".venv\Scripts\haiq.exe"
$vdir = Join-Path $root "secrets"
$vault = Join-Path $vdir "vault.json"
$plain = Join-Path $vdir ".vault-pass.txt"

if (-not (Test-Path $haiqbot)) {
    Write-Host "x 找不到 $haiqbot" -ForegroundColor Red
    Write-Host "  先在项目根目录跑一次 uv sync" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $vault)) {
    Write-Host "x 找不到 $vault" -ForegroundColor Red
    exit 1
}

Write-Host "即将轮换 vault 口令（旧口令 -> 新口令）。"
Write-Host "轮换期间不要有其他进程在读写 vault：先停掉 haiq run / NSSM 服务。" -ForegroundColor Yellow
$confirm = Read-Host "继续？(y/N)"
if ($confirm -ne "y") {
    Write-Host "已取消"
    exit 1
}

# 旧口令：明文文件还在就先用它，避免让用户再抄一遍
if (Test-Path $plain) {
    $env:WEFT_VAULT_PASSPHRASE = (Get-Content $plain -Raw).Trim()
    Write-Host "ok 已从 .vault-pass.txt 载入旧口令（不回显）" -ForegroundColor Green
}
else {
    Write-Host "没有明文密码文件：稍后 haiqbot 会先问你旧口令" -ForegroundColor Yellow
}

Push-Location $root
try {
    & $haiq secrets passwd
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
    # 别把口令留在当前会话环境里
    $env:WEFT_VAULT_PASSPHRASE = $null
}

if ($code -ne 0) {
    Write-Host "x 轮换失败（退出码 $code）；明文文件没有动过" -ForegroundColor Red
    exit $code
}

Write-Host ""
Write-Host "旧口令已失效，vault.json.bak 只有旧口令能解。" -ForegroundColor Yellow

if (Test-Path $plain) {
    $del = Read-Host "新口令已确认可用？删除明文密码文件 secrets\.vault-pass.txt？(y/N)"
    if ($del -eq "y") {
        Remove-Item $plain -Force
        Write-Host "ok 已删除 secrets\.vault-pass.txt" -ForegroundColor Green
    }
    else {
        Write-Host "保留：$plain —— 记住自己删，它让加密形同虚设" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "下一步：把新口令设置到环境变量（三选一）"
Write-Host '  1) 当前会话：  $env:WEFT_VAULT_PASSPHRASE = "<新口令>"'
Write-Host '  2) 用户级永久：setx WEFT_VAULT_PASSPHRASE "<新口令>"   # 写注册表，明文，见下方说明'
Write-Host '  3) 服务：      nssm set <服务名> AppEnvironmentExtra WEFT_VAULT_PASSPHRASE=<新口令>'
Write-Host ""
Write-Host "setx 会把口令明文写进用户注册表（同账户可读），比明文文件好，但不如每次启动时注入。" -ForegroundColor Yellow
Write-Host ""
Write-Host "验证："
Write-Host '  .\.venv\Scripts\haiq.exe doctor'
