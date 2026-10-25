# envpass.ps1 —— 取 vault 主口令：进程环境变量优先，其次本机 .env
#
# 为什么只认这两处：内核不读 .env，口令统一从环境变量 HAIQ_VAULT_PASSPHRASE 拿。
# .env 只是「本机把口令放在哪里」的约定（已在 .gitignore 里），由启动器负责读进环境变量。
# 不再有 DPAPI 文件这类平台专属兜底 —— 换平台就得换一套，代价比收益大。

function Get-EnvFileValue {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Key
    )
    $path = Join-Path $Root ".env"
    if (-not (Test-Path $path)) { return $null }
    foreach ($line in (Get-Content -Path $path -Encoding UTF8)) {
        $t = $line.Trim()
        if (-not $t) { continue }
        if ($t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        if ($t.Substring(0, $i).Trim() -ne $Key) { continue }
        $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
        if ($v) { return $v }
    }
    return $null
}

# 返回口令明文；找不到返回 $null。调用方自己决定「缺了口令是报错还是照跑」。
function Get-VaultPass {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$Key = "HAIQ_VAULT_PASSPHRASE"
    )
    $fromEnv = [Environment]::GetEnvironmentVariable($Key)
    if ($fromEnv) { return $fromEnv }
    return (Get-EnvFileValue -Root $Root -Key $Key)
}

# 缺口令时统一的一句人话（各脚本共用，免得文案漂移）
function Write-VaultPassHint {
    param([string]$Key = "HAIQ_VAULT_PASSPHRASE")
    Write-Host "x 没拿到 vault 主口令（$Key）" -ForegroundColor Red
    Write-Host "  本机：在项目根目录的 .env 里写一行 $Key=<口令>（.env 不入库）" -ForegroundColor Yellow
    Write-Host "  临时：`$env:$Key = '<口令>'（只对当前终端有效）" -ForegroundColor Yellow
    Write-Host "  服务：放进服务环境变量（NSSM: AppEnvironmentExtra；Docker: env_file）" -ForegroundColor Yellow
}
