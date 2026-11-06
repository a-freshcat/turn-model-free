"""生成启动器图标 `launcher/app.ico`（多尺寸，原创绘制）。

为什么要自己画：仓库里没有可用的本机图标，而拿第三方（例如 Antigravity）的图标
当本项目图标既会误导用户、也不该随发行包分发。这里用控制台自己的品牌记号
（纸感底色 + 靛红圆角方块 + 白色 `t`）画一张，尺寸覆盖 16/20/24/32/48/64/128/256。

用法：
    python scripts/make-icon.py            # 写 launcher/app.ico
    python scripts/make-icon.py --check    # 只校验现有 ico 是否与源码一致

只依赖 Pillow（打包桌面版时会用到同一套 .vendor 环境）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "launcher" / "app.ico"
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

# 与控制台样式表里的令牌保持一致（console.css: --accent / --surface）。
ACCENT = (168, 86, 100)
ACCENT_DEEP = (140, 68, 80)
SURFACE = (255, 254, 251)
PAPER = (247, 245, 242)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """优先用系统里稳定的无衬线字体；都没有就退回位图默认字体。"""
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(size: int) -> Image.Image:
    """单张图：外圈纸色留白 -> 靛红圆角方块 -> 白色 t 与下方一条短线。

    `t` 是控制台品牌记号（console.html 的 .brand-mark），不是某个厂商的标识。
    """
    scale = 4  # 先画大再缩，边缘更干净
    big = size * scale
    image = Image.new("RGBA", (big, big), PAPER + (0,))
    draw = ImageDraw.Draw(image)

    pad = round(big * 0.06)
    radius = round(big * 0.24)
    draw.rounded_rectangle(
        (pad, pad, big - pad, big - pad),
        radius=radius,
        fill=ACCENT_DEEP + (255,),
    )
    inner = round(big * 0.035)
    draw.rounded_rectangle(
        (pad + inner, pad + inner, big - pad - inner, big - pad - inner),
        radius=max(2, radius - inner),
        fill=ACCENT + (255,),
    )

    # 字：白色 `t`，基线略高于中心，下面补一条短横线当"纸边"。
    font = _font(round(big * 0.62))
    text = "t"
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((big - tw) / 2 - box[0], (big - th) / 2 - box[1] - big * 0.06),
        text,
        font=font,
        fill=SURFACE + (255,),
    )

    bar_w = round(big * 0.30)
    bar_h = max(2, round(big * 0.045))
    bar_y = round(big * 0.72)
    draw.rounded_rectangle(
        ((big - bar_w) / 2, bar_y, (big + bar_w) / 2, bar_y + bar_h),
        radius=bar_h // 2,
        fill=SURFACE + (235,),
    )

    return image.resize((size, size), Image.LANCZOS)


def build() -> bytes:
    frames = [render(size) for size in SIZES]
    buffer = io.BytesIO()
    frames[-1].save(buffer, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    payload = build()
    digest = hashlib.sha256(payload).hexdigest()
    if args.check:
        if not TARGET.is_file():
            print(f"图标不存在：{TARGET}")
            return 3
        current = TARGET.read_bytes()
        if hashlib.sha256(current).hexdigest() != digest:
            print("图标与源码不一致，重新生成：python scripts/make-icon.py")
            return 3
        print(f"图标一致 {digest[:16]}…")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(payload)
    print(f"已写入 {TARGET}（{len(payload)} 字节，{len(SIZES)} 个尺寸，sha256 {digest[:16]}…）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
