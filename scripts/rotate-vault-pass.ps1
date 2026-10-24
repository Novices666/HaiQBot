# 轮换 haiq vault 口令，并把新口令写进 secrets\.vault-pass.dpapi（DPAPI，仅本机本账户可解）。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\rotate-vault-pass.ps1
#
# 这个脚本不替你决定口令，也不会把口令写进任何日志：
#   - 旧口令：从 secrets\.vault-pass.dpapi（或遗留的明文 .txt）读，只进环境变量，不打印
#   - 新口令：由 haiq secrets passwd 交互输入（两次，不回显）
#   - 轮换成功后，新口令再输一次，用于写 DPAPI 文件（仍然不回显）；写完立刻回读校验

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "vaultpass.ps1")

$haiqbot = Join-Path $root ".venv\Scripts\haiq.exe"
$paths = Get-VaultPassPaths -Root $root
$vault = Join-Path $paths.Dir "vault.json"

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

# 旧口令：DPAPI 文件优先，其次兼容旧明文文件，避免让用户再抄一遍
$old = Read-VaultPass -Root $root
if ($old) {
    $env:HAIQ_VAULT_PASSPHRASE = $old
    $old = $null
    Write-Host "ok 已载入旧口令（DPAPI/明文文件，不回显）" -ForegroundColor Green
}
else {
    Write-Host "没有口令文件：稍后 haiqbot 会先问你旧口令" -ForegroundColor Yellow
}

Push-Location $root
try {
    & $haiqbot secrets passwd
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
    # 别把口令留在当前会话环境里
    $env:HAIQ_VAULT_PASSPHRASE = $null
}

if ($code -ne 0) {
    Write-Host "x 轮换失败（退出码 $code）；口令文件没有动过" -ForegroundColor Red
    exit $code
}

Write-Host ""
Write-Host "旧口令已失效，vault.json.bak 只有旧口令能解。" -ForegroundColor Yellow
Write-Host ""

# 新口令要再输一次才能写进 DPAPI 文件 —— 脚本拿不到 passwd 里输入的内容
$secure = Read-Host "把刚设置的新口令再输一次（写入 DPAPI 文件，不回显）" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $new = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
}
finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if (-not $new) {
    Write-Host "x 空口令：没有写 DPAPI 文件，vault 现在只能用你自己记的口令解" -ForegroundColor Red
    exit 1
}

Protect-VaultPass -Plain $new -Path $paths.Blob
Write-Host ("ok 已写入 " + $paths.Blob + "（" + (Get-Item $paths.Blob).Length + " bytes）") -ForegroundColor Green

# 回读校验：解不开就别删任何东西
$env:HAIQ_VAULT_PASSPHRASE = $new
$new = $null
Push-Location $root
try {
    & $haiqbot secrets list | Out-Null
    $verify = $LASTEXITCODE
}
finally {
    Pop-Location
    $env:HAIQ_VAULT_PASSPHRASE = $null
}

if ($verify -ne 0) {
    Write-Host "x DPAPI 文件解出来的口令打不开 vault（退出码 $verify）—— 别删旧文件，先手动核对" -ForegroundColor Red
    exit $verify
}
Write-Host "ok 用 DPAPI 里的新口令能正常读 vault" -ForegroundColor Green

if (Test-Path $paths.Plain) {
    $del = Read-Host "删除遗留的明文密码文件 secrets\.vault-pass.txt？(y/N)"
    if ($del -eq "y") {
        Remove-Item $paths.Plain -Force
        Write-Host "ok 已删除 secrets\.vault-pass.txt" -ForegroundColor Green
    }
    else {
        Write-Host "保留：$($paths.Plain) —— 记住自己删，它让加密形同虚设" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "启动（口令自动从 DPAPI 文件解，不进命令行）："
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\run-with-vault.ps1"
Write-Host ""
Write-Host "注意：DPAPI 文件只在本机本账户能解。换机器 / 换账户启动时，"
Write-Host "      要先手动设 `$env:HAIQ_VAULT_PASSPHRASE，再跑一次本脚本重建 DPAPI 文件。" -ForegroundColor Yellow
Write-Host ""
Write-Host "NSSM 服务：口令无法从 DPAPI 文件读，需要 nssm set <服务名> AppEnvironmentExtra HAIQ_VAULT_PASSPHRASE=<新口令>"
Write-Host ""
Write-Host "验证："
Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\run-with-vault.ps1 -Foreground'
