"""Genera le icone PWA per web/ — script one-shot, non fa parte dell'app."""
from PIL import Image, ImageDraw, ImageFont

BG = (17, 19, 23)       # ~oklch(0.16 0.01 260)
ACCENT = (52, 199, 132)  # verde terminal
FG = (240, 241, 243)


def make_icon(size: int, path: str, maskable: bool = False):
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)
    pad = int(size * 0.22) if maskable else int(size * 0.14)
    # tre barre stile "market ticker" a altezze diverse
    bar_w = int((size - 2 * pad) / 3 * 0.55)
    gap = int((size - 2 * pad) / 3 * 0.45)
    heights = [0.38, 0.62, 0.5]
    x = pad
    for h in heights:
        bar_h = int((size - 2 * pad) * h)
        y0 = size - pad - bar_h
        y1 = size - pad
        d.rectangle([x, y0, x + bar_w, y1], fill=ACCENT)
        x += bar_w + gap
    img.save(path)


make_icon(192, "web/icon-192.png")
make_icon(512, "web/icon-512.png")
make_icon(512, "web/icon-512-maskable.png", maskable=True)
make_icon(180, "web/apple-touch-icon.png")
print("icone generate")
