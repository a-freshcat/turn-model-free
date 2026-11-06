# 编译免控制台的 Windows 启动器（TurnModelFree.exe）。
#
# 用系统自带的 .NET Framework 编译器：不需要装 SDK，Windows 10/11 上
# C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe 一定存在。
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-launcher.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build-launcher.ps1 -Check
#
# -Check 只验证源码/图标都在并打印将要执行的编译器路径，不产出 exe。

[CmdletBinding()]
param(
    [switch]$Check,
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'launcher\TurnModelFreeLauncher.cs'
$icon = Join-Path $root 'launcher\app.ico'
if (-not $OutFile) { $OutFile = Join-Path $root 'launcher\TurnModelFree.exe' }

if (-not (Test-Path $source)) { throw "缺少源码: $source" }
if (-not (Test-Path $icon)) {
    throw "缺少图标: $icon（先跑 python scripts/make-icon.py）"
}

$csc = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $csc) {
    throw '找不到 csc.exe（.NET Framework 4.x 编译器）。Windows 10/11 默认自带，缺失说明系统组件被裁剪。'
}

$arguments = @(
    '/nologo',
    '/target:winexe',          # 无控制台窗口
    '/platform:anycpu',
    '/optimize+',
    '/codepage:65001',         # 源码里有中文，编译器按 UTF-8 读
    "/win32icon:$icon",
    "/out:$OutFile",
    '/reference:System.Windows.Forms.dll',
    '/reference:System.Drawing.dll',
    $source
)

if ($Check) {
    Write-Output "csc: $csc"
    Write-Output "source: $source"
    Write-Output "icon: $icon"
    Write-Output "out: $OutFile"
    exit 0
}

& $csc @arguments
if ($LASTEXITCODE -ne 0) { throw "csc 失败（退出码 $LASTEXITCODE）" }

$item = Get-Item $OutFile
$hash = (Get-FileHash $OutFile -Algorithm SHA256).Hash.ToLower()
Write-Output ("built {0} ({1} bytes, sha256 {2})" -f $item.FullName, $item.Length, $hash.Substring(0, 16) + '…')
