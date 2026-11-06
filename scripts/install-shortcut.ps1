# 在桌面（可选开始菜单）创建指向 TurnModelFree.exe 的快捷方式。
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -StartMenu
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -Exe "D:\app\TurnModelFree.exe"
#
# 快捷方式只是「怎么启动」，不复制任何文件：它指向发布目录里的 exe，
# 所以换版本（重新解压）后重新跑一次这条命令即可。
# 参数会原样带上，例如：
#   ... -Arguments '--port 8300'
#
# `-Exe` 用于指到另一份安装（例如仓库里编了 exe、但真正在跑的是带 `.venv` 的
# 用户目录那份）。指向哪份就用哪份的依赖，别把快捷方式指到缺依赖的树上。

[CmdletBinding()]
param(
    [switch]$StartMenu,
    [switch]$Remove,
    [string]$Arguments = '',
    [string]$Name = 'turn-model-free',
    [string]$Exe = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $root 'launcher\TurnModelFree.exe' }
$exe = $Exe

if (-not (Test-Path $exe)) {
    throw "找不到启动器: $exe（先跑 scripts\build-launcher.ps1，或用 -Exe 指到已安装的那一份）"
}
$exe = (Resolve-Path $exe).Path
# 工作目录取 exe 所在目录：启动器自己会往上/往下找 gateway，但 cwd 是它判断
# 相对位置的第一手信息，指向别处会让三种落法里最靠前的那种失配。
$working = Split-Path -Parent $exe

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
    $shortcut.WorkingDirectory = $working
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = 'turn-model-free 桌面控制台（本机模型网关）'
    $shortcut.Arguments = $Arguments
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Output "created $link -> $exe"
}

# 桌面图标缓存偶尔会留着旧图标；这不是错误，只是提示一下。
Write-Output '如果桌面图标还是旧的，注销一次或重启资源管理器即可刷新。'
