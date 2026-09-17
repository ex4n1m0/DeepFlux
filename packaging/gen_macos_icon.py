"""Generate packaging/icon.icns from the root DeepFlux4.png.

The macOS build (packaging/app_macos.spec) needs an .icns; everything else
icon-related derives from packaging/gen_logo_assets.py (Windows targets).
Pillow writes the full Apple icon size set by downscaling the source, so
keep the source >= 512x512 (DeepFlux4.png is 1024).

Run from anywhere:  python packaging/gen_macos_icon.py
"""
from pathlib import Path

from PIL import Image

root = Path(__file__).resolve().parent.parent
src = root / "DeepFlux4.png"
out = root / "packaging" / "icon.icns"

img = Image.open(src).convert("RGBA")
img.save(out, format="ICNS")
print(f"wrote {out}")
