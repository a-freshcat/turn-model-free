"""打包桌面版与网页版（从 Git 索引取文件，不夹带本机凭据）。

运行：python scripts/prepare_release.py --vendor-dir gateway/.vendor
若 checkout 没有 .vendor，可传 --vendor-archive 旧版 desktop zip。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / '.release'
REQUIRED = {
    'gateway/main.py', 'gateway/launch_desktop.py', 'gateway/plugin_gateway.py',
    'gateway/scripts/pack_bridge.mjs', 'gateway/scripts/pack_gateway.mjs',
    'gateway/scripts/patch-raccoon-cooldown.mjs',
    'gateway/web/console.html', 'gateway/web/console.css', 'gateway/web/console.mjs',
    'launch-desktop.cmd', 'gateway/launch-desktop.cmd',
}
EXCLUDE_PREFIXES = ('docs/ui-prototype/',)
EXCLUDE_FILES = {'gateway/web/prototype.html'}


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
                vendor_archive: Path | None):
    label = f'turn-model-free-{flavor}-v{version}'
    target = DIST / f'{label}.zip'
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=7) as out:
        for rel in files:
            out.write(ROOT / rel, f'{label}/{rel}')
        if flavor == 'desktop':
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
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (DIST / f'{label}.sha256.txt').write_text(f'{digest}  {target.name}\n', encoding='ascii')
    print(json.dumps({'file': str(target), 'bytes': target.stat().st_size,
                      'sha256': digest, 'files': len(entries)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vendor-dir', type=Path)
    parser.add_argument('--vendor-archive', type=Path)
    args = parser.parse_args()
    version = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    files = source_paths()
    DIST.mkdir(exist_ok=True)
    for flavor in ('desktop', 'web'):
        zip_package(flavor, version, files, args.vendor_dir, args.vendor_archive)


if __name__ == '__main__':
    main()
