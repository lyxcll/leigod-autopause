#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 leigod_autopause.py 打包成单文件 exe（PyInstaller）。

用法：
    pip install pyinstaller pillow
    python build_exe.py            # 产物 dist/LeigodAutoPause.exe

CI 里（.github/workflows/build-exe.yml）也是跑这个脚本，所以本地和 CI 的产物一致。
"""
from __future__ import annotations

import os
import subprocess
import sys

# Windows 的 CI / 老终端默认 cp1252/cp936，打 emoji 会 UnicodeEncodeError，统一切成 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(ROOT, "assets")
APP_NAME = "LeigodAutoPause"
VERSION = (1, 1, 0, 0)


def make_icon(path: str) -> str | None:
    """画一个「紫色圆角方块 + 白色暂停条」的图标，顺便生成多尺寸 ico。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("⚠️ 没装 Pillow，跳过图标（exe 会用默认图标）")
        return None

    scale = 4
    size = 256 * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 竖向渐变背景
    top, bottom = (109, 91, 208), (58, 45, 160)
    for y in range(size):
        t = y / size
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        d.line([(0, y), (size, y)], fill=color)

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255
    )
    img.putalpha(mask)

    # 两根白色暂停条
    bar_w, bar_h = int(size * 0.16), int(size * 0.46)
    gap = int(size * 0.12)
    cy = size // 2
    for dx in (-gap // 2 - bar_w // 2, gap // 2 + bar_w // 2):
        x = size // 2 + dx
        d.rounded_rectangle(
            [x - bar_w // 2, cy - bar_h // 2, x + bar_w // 2, cy + bar_h // 2],
            radius=bar_w // 4,
            fill=(255, 255, 255, 255),
        )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [img.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in sizes],
                    append_images=frames[:-1])
    print(f"🎨 图标已生成：{path}")
    return path


VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, {v3}),
    prodvers=({v0}, {v1}, {v2}, {v3}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'lyxcll'),
        StringStruct('FileDescription', '雷神加速器挂机自动暂停'),
        StringStruct('FileVersion', '{v}.0'),
        StringStruct('InternalName', '{app}'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', '{app}.exe'),
        StringStruct('ProductName', '雷神加速器挂机自动暂停'),
        StringStruct('ProductVersion', '{v}.0')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def main() -> int:
    os.chdir(ROOT)
    icon = make_icon(os.path.join(ASSETS, "app.ico"))

    vinfo = os.path.join(ROOT, "build", "version_info.txt")
    os.makedirs(os.path.dirname(vinfo), exist_ok=True)
    with open(vinfo, "w", encoding="utf-8") as f:
        f.write(VERSION_INFO.format(
            app=APP_NAME,
            v=".".join(str(x) for x in VERSION[:3]),
            v0=VERSION[0], v1=VERSION[1], v2=VERSION[2], v3=VERSION[3],
        ))

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        f"--name={APP_NAME}",
        f"--version-file={vinfo}",
        # 这些模块用不到，排掉能小一点、启动快一点
        "--exclude-module=tkinter",
        "--exclude-module=unittest",
        "--exclude-module=pydoc",
        "--exclude-module=lib2to3",
        "--exclude-module=test",
        "leigod_autopause.py",
    ]
    if icon:
        args.insert(-1, f"--icon={icon}")
    print("🔨 " + " ".join(args[2:]))
    r = subprocess.run(args)
    if r.returncode != 0:
        return r.returncode

    exe = os.path.join(ROOT, "dist", f"{APP_NAME}.exe")
    size_mb = os.path.getsize(exe) / 1024 / 1024
    print(f"\n✅ 打包完成：{exe}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
