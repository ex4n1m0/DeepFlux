"""Generate Inno Setup wizard images from the DeepFlux logo."""
from PIL import Image

BG = (0, 0, 0)  # pure black — DeepFlux.png background is black; matches WizardBackColor

logo = Image.open("DeepFlux.png").convert("RGBA")

# Left panel image: 164x314, logo centered with margins.
wizard = Image.new("RGB", (164, 314), BG)
side = 140
resized = logo.resize((side, side), Image.LANCZOS)
wizard.paste(resized, ((164 - side) // 2, (314 - side) // 2), resized)
wizard.save("packaging/wizard_image.png")

# Top-right small image: 55x55.
small = Image.new("RGB", (55, 55), BG)
side_s = 48
resized_s = logo.resize((side_s, side_s), Image.LANCZOS)
small.paste(resized_s, ((55 - side_s) // 2, (55 - side_s) // 2), resized_s)
small.save("packaging/wizard_small.png")

print("wrote packaging/wizard_image.png and packaging/wizard_small.png")
