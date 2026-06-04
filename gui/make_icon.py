"""Generate gui/icon.ico (and gui/web/logo.png) from the NexRush brand image.

Source art is `gui/logo_source.png` (the NexRush avatar). This frames it in a
rounded square with a cyan hairline to match the app theme, then writes a
multi-resolution `.ico` for the window/taskbar/exe and a small PNG for the
in-app sidebar.

Requires Pillow (design-time only, not a runtime dependency):
    pip install pillow
    python gui/make_icon.py
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "logo_source.png")
ICON = os.path.join(HERE, "icon.ico")
WEB_LOGO = os.path.join(HERE, "web", "logo.png")

CYAN = (56, 214, 248, 255)
ICO_SIZES = [256, 128, 64, 48, 32, 16]
SS = 4  # supersample for crisp edges


def _rounded(img_size, radius):
    m = Image.new("L", (img_size, img_size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, img_size - 1, img_size - 1], radius=radius, fill=255
    )
    return m


def framed(size):
    """Return an `size`x`size` RGBA: the avatar, square-cropped, rounded, with a
    cyan hairline border."""
    big = size * SS
    src = Image.open(SOURCE).convert("RGBA")
    side = min(src.size)
    # center square crop, then cover-fit to the canvas
    left = (src.width - side) // 2
    top = (src.height - side) // 2
    src = src.crop((left, top, left + side, top + side)).resize((big, big), Image.LANCZOS)

    radius = int(big * 0.225)
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    canvas.paste(src, (0, 0))
    canvas.putalpha(_rounded(big, radius))

    # cyan hairline just inside the edge (scaled so it survives downsampling)
    d = ImageDraw.Draw(canvas)
    inset = int(big * 0.03)
    d.rounded_rectangle(
        [inset, inset, big - inset, big - inset],
        radius=int(radius * 0.85), outline=CYAN, width=max(SS * 2, big // 90),
    )
    # re-apply outer mask so the border keeps the rounded silhouette
    canvas.putalpha(_rounded(big, radius))
    return canvas.resize((size, size), Image.LANCZOS)


def main():
    base = framed(256)
    frames = {s: framed(s) for s in ICO_SIZES}
    # Save a true multi-resolution ICO (each size rendered, not just downscaled).
    base.save(
        ICON, format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[frames[s] for s in ICO_SIZES if s != 256],
    )
    print(f"Wrote {ICON} ({os.path.getsize(ICON)} bytes)")

    os.makedirs(os.path.dirname(WEB_LOGO), exist_ok=True)
    framed(128).save(WEB_LOGO)
    print(f"Wrote {WEB_LOGO}")


if __name__ == "__main__":
    main()
