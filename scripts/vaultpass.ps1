# vaultpass.ps1 —— vault 口令文件的读写（DPAPI，仅本机本账户可解）
#
# 被 dev-up.ps1 / run-with-vault.ps1 / rotate-vault-pass.ps1 用 dot-source 引用：
#   . (Join-Path $PSScriptRoot "vaultpass.ps1")
#
# 为什么不存明文：DPAPI(CurrentUser) 的密钥在用户配置单元里，文件即使被拷走、
# 被同步进网盘、被备份到别的机器，都解不开。口令只在进程环境变量里活一次。

Add-Type -AssemblyName System.Security -ErrorAction SilentlyContinue

function Get-VaultPassPaths {
    param([Parameter(Mandatory = $true)][string]$Root)
    $dir = Join-Path $Root "secrets"
    [pscustomobject]@{
        Dir   = $dir
        Blob  = Join-Path $dir ".vault-pass.dpapi"
        Plain = Join-Path $dir ".vault-pass.txt"
    }
}

function Protect-VaultPass {
    param(
        [Parameter(Mandatory = $true)][string]$Plain,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Plain)
    $blob = [System.Security.Cryptography.ProtectedData]::Protect(
        $bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, [Convert]::ToBase64String($blob), $utf8NoBom)
}

function Unprotect-VaultPass {
    param([Parameter(Mandatory = $true)][string]$Path)
    $b64 = [System.IO.File]::ReadAllText($Path).Trim()
    $blob = [Convert]::FromBase64String($b64)
    $bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $blob, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    return [System.Text.Encoding]::UTF8.GetString($bytes).Trim()
}

# 返回口令明文（不带换行）；找不到返回 $null。
# 优先 DPAPI 文件；为兼容老仓库仍认明文文件。
function Read-VaultPass {
    param([Parameter(Mandatory = $true)][string]$Root)
    $p = Get-VaultPassPaths -Root $Root
    if (Test-Path $p.Blob) { return (Unprotect-VaultPass -Path $p.Blob) }
    if (Test-Path $p.Plain) { return ((Get-Content -Raw $p.Plain).Trim()) }
    return $null
}
