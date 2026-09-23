"""Regenerate every logo-derived asset from the root DeepFlux.png.

Run from the repo root after updating DeepFlux.png:

    python packaging/gen_logo_assets.py

Outputs:
  packaging/icon.ico                 — exe icon (app.spec) + installer icon
  packaging/logo_48.png              — tray icon / in-app small logo
  packaging/wizard_image.png         — Inno wizard left panel (164x314, black bg)
  packaging/wizard_small.png         — Inno wizard top-right (55x55, black bg)
  chrome_extension/icons/*.png       — browser extension icons (16/48/128)
  website/deepflux/DeepFlux.webp     — website logo (512px, keeps alpha)

Since 5.2 the source is the ORIGINAL DeepFlux.png (owner decision
2026-09-23: the 5.0-era DeepFlux5.png art is retired from the app icon
and the landing hero). It carries real alpha; the wizard images paste it
onto the installer's WizardBackColor (#000000). Supersedes
gen_wizard_images.py.
"""
from PIL import Image

BG = (0, 0, 0)  # installer wizard pages are pure black (WizardBackColor)

logo = Image.open("DeepFlux.png").convert("RGBA")


def resized(side: int) -> Image.Image:
    return logo.resize((side, side), Image.LANCZOS)


def paste_on_black(size: tuple[int, int], side: int) -> Image.Image:
    canvas = Image.new("RGB", size, BG)
    art = resized(side)
    canvas.paste(art, ((size[0] - side) // 2, (size[1] - side) // 2), art)
    return canvas


# --- App + installer icons ---
logo.save(
    "packaging/icon.ico",
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("wrote packaging/icon.ico")

resized(48).save("packaging/logo_48.png")
print("wrote packaging/logo_48.png")

# --- Inno Setup wizard images ---
paste_on_black((164, 314), 140).save("packaging/wizard_image.png")
paste_on_black((55, 55), 48).save("packaging/wizard_small.png")
print("wrote packaging/wizard_image.png and packaging/wizard_small.png")

# --- Chrome extension icons ---
for side in (16, 48, 128):
    resized(side).save(f"chrome_extension/icons/{side}.png")
print("wrote chrome_extension/icons/16.png, 48.png, 128.png")

# --- Website logo (hero mark at NATIVE 1254px — the hero displays it at
# min(1254px, column width); also the 26px nav brand + favicon) ---
# Filename differs from the retired DeepFlux5.webp so a cached CDN copy of
# the old art can never be served.
logo.save("website/deepflux/DeepFlux.webp", format="WEBP", quality=90)
print("wrote website/deepflux/DeepFlux.webp")
