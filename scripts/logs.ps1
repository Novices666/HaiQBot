# logs.ps1 —— 追 haiqbot 的运行日志（默认 logs\haiq.out）
#
# 为什么需要它：
#   run-with-env.ps1 把 python 的 stdout / stderr 全部重定向进日志文件，
#   所以 dev-up.ps1 拉起的那个黑框窗口里永远没有内容。
#   想看实时输出，就另开一个终端跑这个脚本（黑框保持不动，关掉它等于杀 python）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\logs.ps1
#   ... -Tail 200             # 先回看最后 200 行，再继续追
#   ... -Pattern error        # 只显示匹配的行（不区分大小写，可正则）
#   ... -Pattern 'handler_error|media'
#   ... -Once                 # 只看当前尾部就退出，不追
#   ... -Log logs\other.out   # 换一个文件（也接受绝对路径）

param(
    [int]$Tail = 50,
    [string]$Pattern = "",
    [switch]$Once,
    [string]$Log = "logs\haiq.out"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if ([System.IO.Path]::IsPathRooted($Log)) { $logPath = $Log }
else { $logPath = Join-Path $root $Log }

# 日志是 UTF-8；控制台默认 GBK，不设的话中文提示会乱码
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

if (-not (Test-Path $logPath)) {
    if ($Once) {
        Write-Host "x 日志不存在：$logPath" -ForegroundColor Red
        exit 1
    }
    Write-Host "! 日志还不存在，等它出现：$logPath" -ForegroundColor Yellow
    Write-Host "  haiqbot 还没起过？先跑 scripts\dev-up.ps1"
    while (-not (Test-Path $logPath)) { Start-Sleep -Milliseconds 500 }
}

Write-Host "== $logPath" -ForegroundColor Cyan
if ($Pattern) { Write-Host "== 过滤 /$Pattern/（不区分大小写）" -ForegroundColor Cyan }
if (-not $Once) { Write-Host "   Ctrl+C 停止" -ForegroundColor DarkGray }
Write-Host ""

# Get-Content 的原生错误不该因 EAP=Stop 变成终止异常
$ErrorActionPreference = "Continue"

if ($Once) {
    if ($Pattern) { Get-Content -Path $logPath -Encoding UTF8 -Tail $Tail | Where-Object { $_ -match $Pattern } }
    else { Get-Content -Path $logPath -Encoding UTF8 -Tail $Tail }
}
else {
    if ($Pattern) { Get-Content -Path $logPath -Encoding UTF8 -Tail $Tail -Wait | Where-Object { $_ -match $Pattern } }
    else { Get-Content -Path $logPath -Encoding UTF8 -Tail $Tail -Wait }
}
