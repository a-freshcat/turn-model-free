"""打包桌面版与网页版（从 Git 索引取文件，不夹带本机凭据）。

运行：python scripts/prepare_release.py --vendor-dir gateway/.vendor
若 checkout 没有 .vendor，可传 --vendor-archive 旧版 desktop zip。

桌面版的免控制台启动器（`TurnModelFree.exe`）是**现编**的：它由
`launcher/TurnModelFreeLauncher.cs` + `launcher/app.ico` 用系统自带的 csc
编译，几十 KB，没必要把二进制放进仓库。编译不出来（系统被裁掉 .NET
Framework）时打包照常完成，只是不带 exe —— 那时仍可用 launch-desktop.cmd。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / '.release'
LAUNCHER_SOURCE = ROOT / 'launcher' / 'TurnModelFreeLauncher.cs'
LAUNCHER_ICON = ROOT / 'launcher' / 'app.ico'
REQUIRED = {
    'gateway/main.py', 'gateway/launch_desktop.py', 'gateway/plugin_gateway.py',
    'gateway/scripts/pack_bridge.mjs', 'gateway/scripts/pack_gateway.mjs',
    'gateway/scripts/patch-raccoon-cooldown.mjs',
    'gateway/web/console.html', 'gateway/web/console.css', 'gateway/web/console.mjs',
    'launch-desktop.cmd', 'gateway/launch-desktop.cmd',
    'launcher/TurnModelFreeLauncher.cs', 'launcher/app.ico',
    'scripts/build-launcher.ps1', 'scripts/install-shortcut.ps1', 'scripts/make-icon.py',
}
EXCLUDE_PREFIXES = ('docs/ui-prototype/',)
EXCLUDE_FILES = {'gateway/web/prototype.html'}


def build_launcher(target: Path) -> str:
    """编一份 winexe 启动器到 target。失败返回原因（打包继续，只是不带 exe）。"""
    if not LAUNCHER_SOURCE.is_file() or not LAUNCHER_ICON.is_file():
        return '缺少 launcher 源码或图标'
    roots = [Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET' / part
             / 'v4.0.30319' / 'csc.exe'
             for part in ('Framework64', 'Framework')]
    csc = next((path for path in roots if path.is_file()), None)
    if csc is None:
        return '找不到 csc.exe（.NET Framework 4.x）'
    result = subprocess.run([
        str(csc), '/nologo', '/target:winexe', '/platform:anycpu', '/optimize+',
        '/codepage:65001', f'/win32icon:{LAUNCHER_ICON}', f'/out:{target}',
        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
        str(LAUNCHER_SOURCE),
    ], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0 or not target.is_file():
        detail = (result.stdout + result.stderr).strip().splitlines()
        return f'csc 失败：{detail[0] if detail else result.returncode}'
    return ''


def source_paths():
    raw = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT)
    names = {name.decode('utf-8').replace('\\', '/') for name in raw.split(b'\0') if name}
    missing = REQUIRED - names
    if missing:
        raise RuntimeError(f'提交索引缺少发行所需文件：{sorted(missing)}')
    return sorted(name for name in names
                  if not name.startswith(EXCLUDE_PREFIXES)
                  and name not in EXCLUDE_FILES
                  and (ROOT / name).is_file())


def zip_package(flavor: str, version: str, files, vendor_dir: Path | None,
                vendor_archive: Path | None, launcher: Path | None):
    label = f'turn-model-free-{flavor}-v{version}'
    target = DIST / f'{label}.zip'
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=7) as out:
        for rel in files:
            out.write(ROOT / rel, f'{label}/{rel}')
        if flavor == 'desktop':
            # 免控制台启动器放在包根：与 gateway\ 同级，双击即开，
            # 也正好是 install-shortcut.ps1 期待的落点。
            if launcher is not None:
                out.write(launcher, f'{label}/TurnModelFree.exe')
            if vendor_dir and (vendor_dir / 'fastapi' / '__init__.py').is_file():
                for path in sorted(vendor_dir.rglob('*')):
                    if path.is_file() and '__pycache__' not in path.parts:
                        rel = path.relative_to(vendor_dir).as_posix()
                        out.write(path, f'{label}/gateway/.vendor/{rel}')
            elif vendor_archive:
                with zipfile.ZipFile(vendor_archive) as prior:
                    for entry in prior.infolist():
                        marker = '/gateway/.vendor/'
                        if marker not in entry.filename or entry.is_dir():
                            continue
                        rel = entry.filename.split(marker, 1)[1]
                        if '__pycache__' in rel.split('/'):
                            continue
                        out.writestr(f'{label}/gateway/.vendor/{rel}', prior.read(entry))
            else:
                raise RuntimeError('桌面包需要 --vendor-dir 或 --vendor-archive')
    with zipfile.ZipFile(target) as check:
        entries = set(check.namelist())
        assert all(f'{label}/{rel}' in entries for rel in REQUIRED)
        assert not any('/.gateway-api-key' in name or '/accounts.json' in name
                       or '/.gateway-auth/' in name or '/.pack/' in name
                       for name in entries)
        if flavor == 'desktop':
            assert f'{label}/gateway/.vendor/fastapi/__init__.py' in entries
            if launcher is not None:
                assert f'{label}/TurnModelFree.exe' in entries
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (DIST / f'{label}.sha256.txt').write_text(f'{digest}  {target.name}\n', encoding='ascii')
    print(json.dumps({'file': str(target), 'bytes': target.stat().st_size,
                      'sha256': digest, 'files': len(entries),
                      'launcher': f'{label}/TurnModelFree.exe' if launcher else None},
                     ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vendor-dir', type=Path)
    parser.add_argument('--vendor-archive', type=Path)
    parser.add_argument('--no-launcher', action='store_true',
                        help='不编免控制台启动器（默认会编；编不出来时打包照常完成）')
    args = parser.parse_args()
    version = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    files = source_paths()
    DIST.mkdir(exist_ok=True)

    launcher: Path | None = None
    staged = Path(tempfile.mkdtemp(prefix='tmf-launcher-'))
    try:
        if not args.no_launcher:
            candidate = staged / 'TurnModelFree.exe'
            problem = build_launcher(candidate)
            if problem:
                print(f'[launcher] 未包含启动器：{problem}'
                      '（launch-desktop.cmd 仍可用）', file=sys.stderr)
            else:
                launcher = candidate
        for flavor in ('desktop', 'web'):
            zip_package(flavor, version, files, args.vendor_dir, args.vendor_archive,
                        launcher if flavor == 'desktop' else None)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


if __name__ == '__main__':
    main()
