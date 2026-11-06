# 在桌面（可选开始菜单）创建指向 TurnModelFree.exe 的快捷方式。
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -StartMenu
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -Remove
#
# 快捷方式只是「怎么启动」，不复制任何文件：它指向发布目录里的 exe，
# 所以换版本（重新解压）后重新跑一次这条命令即可。
# 参数会原样带上，例如：
#   ... -Arguments '--port 8300'

[CmdletBinding()]
param(
    [switch]$StartMenu,
    [switch]$Remove,
    [string]$Arguments = '',
    [string]$Name = 'turn-model-free'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'launcher\TurnModelFree.exe'

if (-not (Test-Path $exe)) {
    throw "找不到启动器: $exe（先跑 scripts\build-launcher.ps1）"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$targets = @(Join-Path $desktop "$Name.lnk")
if ($StartMenu) {
    $programs = [Environment]::GetFolderPath('Programs')
    if ($programs) { $targets += Join-Path $programs "$Name.lnk" }
}

$shell = New-Object -ComObject WScript.Shell
foreach ($link in $targets) {
    if ($Remove) {
        if (Test-Path $link) { Remove-Item $link -Force; Write-Output "removed $link" }
        continue
    }
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = 'turn-model-free 桌面控制台（本机模型网关）'
    $shortcut.Arguments = $Arguments
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Output "created $link -> $exe"
}

# 桌面图标缓存偶尔会留着旧图标；这不是错误，只是提示一下。
Write-Output '如果桌面图标还是旧的，注销一次或重启资源管理器即可刷新。'
