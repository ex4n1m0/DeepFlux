"""Generate the DeepFlux 5.0 brand art from scratch (deterministic, PIL-only).

Outputs:
  DeepFlux5.png               — 1024x1024 app logo, black bg baked in (the
                                gen_logo_assets.py pipeline requires it: the
                                installer wizard pastes onto pure black, and
                                small sizes must not gain a dark frame).
  website/deepflux/DeepFluxBanner.webp — 1280x669 hero wordmark, PURE BLACK
                                at every edge pixel (mix-blend-mode: screen on
                                the website drops the black; any non-zero edge
                                brings back a hard rectangle).

Design: the v5 mark is a "flux" of three ascending streaks (cyan -> blue ->
violet, the app's accent ramp) over a deep-space glow, with the DeepFlux
wordmark in the bundled Inter. Run once per brand refresh; derived icons
come from packaging/gen_logo_assets.py.
"""
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SS = 4  # supersample factor for crisp diagonals/rounded ends
FONT = "packaging/fonts/Inter.ttf"

CYAN = (127, 216, 255)     # #7fd8ff
TURQ = (168, 237, 255)     # #a8edff — the app's active accent
BLUE = (79, 125, 249)      # #4f7df9
VIOLET = (139, 92, 246)    # #8b5cf6


def _font(px: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT, px)
    try:
        f.set_variation_by_name("Bold" if bold else "Regular")
    except Exception:
        pass
    return f


def _glow_add(base: Image.Image, layer: Image.Image, color, radius: int) -> Image.Image:
    """Screen-add a blurred, colorized copy of `layer` onto `base` (neon)."""
    glow = layer.filter(ImageFilter.GaussianBlur(radius))
    tinted = Image.new("RGB", base.size, color)
    colored = Image.composite(tinted, Image.new("RGB", base.size, (0, 0, 0)), glow)
    return ImageChops.screen(base, colored)


def _streak_layer(size, p1, p2, width, color, glow_radius):
    """One rounded diagonal streak + its glow on a black canvas."""
    img = Image.new("RGB", size, (0, 0, 0))
    lay = Image.new("L", size, 0)
    d = ImageDraw.Draw(lay)
    d.line([p1, p2], fill=255, width=width)
    r = width // 2
    for (x, y) in (p1, p2):
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    solid = Image.composite(Image.new("RGB", size, color),
                            Image.new("RGB", size, (0, 0, 0)), lay)
    img = _glow_add(img, lay, color, glow_radius)
    return ImageChops.screen(img, solid)


def draw_mark(img: Image.Image, cx: float, cy: float, span: float) -> None:
    """Three ascending flux streaks centered around (cx, cy).

    `span` is a fraction of the canvas WIDTH and applies to BOTH axes (one
    pixel scale), so the streaks keep their 45°-ish slant on any aspect
    ratio — multiplying the y axis by the height instead would shear them
    (and, on wide canvases, shove the mark off the bottom edge).
    """
    W, H = img.size
    span_px = span * W
    coords = [
        # (dx1, dy1, dx2, dy2) relative to span, longest streak first
        (-0.50, +0.20, +0.50, -0.26),
        (-0.30, +0.38, +0.34, -0.02),
        (-0.08, +0.52, +0.16, +0.20),
    ]
    colors = [CYAN, BLUE, VIOLET]
    for (dx1, dy1, dx2, dy2), col in zip(coords, colors):
        p1 = (int(cx * W + dx1 * span_px), int(cy * H + dy1 * span_px))
        p2 = (int(cx * W + dx2 * span_px), int(cy * H + dy2 * span_px))
        streak = _streak_layer(img.size, p1, p2, int(0.11 * span_px), col,
                               int(0.035 * span_px))
        img.paste(ImageChops.screen(img, streak), (0, 0))


def gradient_text(size, text, font, colors) -> Image.Image:
    """Render `text` filled with a horizontal multi-stop gradient on black."""
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    bbox = d.textbbox((0, 0), text, font=font)
    pos = ((size[0] - (bbox[2] - bbox[0])) // 2 - bbox[0],
           (size[1] - (bbox[3] - bbox[1])) // 2 - bbox[1])
    d.text(pos, text, font=font, fill=255)
    grad = Image.new("RGB", size)
    gd = ImageDraw.Draw(grad)
    n = len(colors) - 1
    for x in range(size[0]):
        t = x / max(1, size[0] - 1) * n
        i = min(int(t), n - 1)
        f = t - i
        c = tuple(round(colors[i][k] * (1 - f) + colors[i + 1][k] * f) for k in range(3))
        gd.line([(x, 0), (x, size[1])], fill=c)
    return Image.composite(grad, Image.new("RGB", size, (0, 0, 0)), mask)


def backdrop(size) -> Image.Image:
    """Near-black canvas with a faint blue/violet nebula, PURE BLACK edges."""
    W, H = size
    img = Image.new("RGB", size, (0, 0, 0))
    blob = Image.new("L", size, 0)
    d = ImageDraw.Draw(blob)
    d.ellipse([W * 0.28, H * 0.16, W * 0.72, H * 0.60], fill=52)
    blob = blob.filter(ImageFilter.GaussianBlur(W * 0.09))
    # Gaussian tails could kiss the canvas edge — fade the outer ring to
    # exact black (the banner's screen-blend contract depends on it).
    fade = Image.new("L", size, 0)
    fd = ImageDraw.Draw(fade)
    fd.rounded_rectangle([W * 0.04, H * 0.04, W * 0.96, H * 0.96],
                         radius=int(W * 0.08), fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(W * 0.02))
    blob = ImageChops.multiply(blob, fade)
    return ImageChops.screen(img, Image.composite(
        Image.new("RGB", size, (18, 26, 58)), img, blob))


def _fit_font(text: str, max_w: int, start_px: int) -> ImageFont.FreeTypeFont:
    """Largest bold Inter whose rendered width fits max_w (shrink loop)."""
    px = start_px
    while px > 8:
        f = _font(px)
        box = ImageDraw.Draw(Image.new("L", (8, 8))).textbbox((0, 0), text, font=f)
        if box[2] - box[0] <= max_w:
            return f
        px = int(px * 0.94)
    return _font(8)


def make_logo(path: str) -> None:
    W = 1024 * SS
    img = backdrop((W, W))
    draw_mark(img, cx=0.5, cy=0.40, span=0.62)
    font = _fit_font("DeepFlux", int(W * 0.86), int(W * 0.095))
    text = gradient_text((W, int(W * 0.16)), "DeepFlux", font,
                         [TURQ, CYAN, (238, 244, 255)])
    img.paste(ImageChops.screen(img, text), (0, int(W * 0.66)))
    img = img.resize((1024, 1024), Image.LANCZOS)
    img.save(path)
    print("wrote", path)


def make_banner(path: str) -> None:
    W, H = 1280 * SS, 669 * SS
    img = backdrop((W, H))
    # Mark left of the wordmark, both vertically centered.
    draw_mark(img, cx=0.225, cy=0.52, span=0.30)
    tw, th = int(W * 0.64), H
    font = _fit_font("DeepFlux", int(tw * 0.94), int(H * 0.34))
    text = gradient_text((tw, th), "DeepFlux", font,
                         [TURQ, CYAN, (238, 244, 255)])
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    canvas.paste(text, (int(W * 0.34), 0))
    img = ImageChops.screen(img, canvas)
    img = img.resize((1280, 669), Image.LANCZOS)
    img.save(path, format="WEBP", quality=92)
    print("wrote", path)


if __name__ == "__main__":
    make_logo("DeepFlux5.png")
    make_banner("website/deepflux/DeepFluxBanner.webp")
