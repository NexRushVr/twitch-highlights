"""Generate gui/icon.ico with no third-party deps (pure stdlib).

Draws a rounded-square gradient (magenta -> cyan, matching the app theme) with a
white play triangle, at 256/48/32/16 px, and packs them into a PNG-payload .ico.

Run once to (re)create the icon:  python gui/make_icon.py
"""

import math
import os
import struct
import zlib

MAGENTA = (197, 75, 214)
CYAN = (56, 189, 248)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def render(size, ss=4):
    """Return raw RGBA bytes for an icon of `size` px, supersampled `ss`x for AA."""
    out = bytearray(size * size * 4)
    c = size / 2.0
    half = size * 0.44          # half-extent of the rounded square
    radius = size * 0.22        # corner radius
    inner = half - radius
    # play-triangle vertices (pointing right)
    tx = (size * 0.41, size * 0.41, size * 0.70)
    ty = (size * 0.31, size * 0.69, size * 0.50)

    def in_tri(x, y):
        def cross(ax, ay, bx, by, px, py):
            return (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        d1 = cross(tx[0], ty[0], tx[1], ty[1], x, y)
        d2 = cross(tx[1], ty[1], tx[2], ty[2], x, y)
        d3 = cross(tx[2], ty[2], tx[0], ty[0], x, y)
        has_neg = d1 < 0 or d2 < 0 or d3 < 0
        has_pos = d1 > 0 or d2 > 0 or d3 > 0
        return not (has_neg and has_pos)

    def in_rrect(x, y):
        dx = abs(x - c) - inner
        dy = abs(y - c) - inner
        ox, oy = max(dx, 0.0), max(dy, 0.0)
        dist = math.hypot(ox, oy) + min(max(dx, dy), 0.0) - radius
        return dist <= 0.0

    samples = ss * ss
    for py in range(size):
        for px in range(size):
            inside = 0
            r = g = b = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = px + (sx + 0.5) / ss
                    y = py + (sy + 0.5) / ss
                    if not in_rrect(x, y):
                        continue
                    inside += 1
                    if in_tri(x, y):
                        r += 255; g += 255; b += 255
                    else:
                        col = _lerp(MAGENTA, CYAN, (x + y) / (2 * size))
                        r += col[0]; g += col[1]; b += col[2]
            i = (py * size + px) * 4
            if inside:
                out[i] = r // inside
                out[i + 1] = g // inside
                out[i + 2] = b // inside
                out[i + 3] = (inside * 255) // samples
            # else: fully transparent (already 0)
    return bytes(out)


def png_bytes(size, rgba):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = bytearray()
    row = size * 4
    for y in range(size):
        raw.append(0)  # filter: none
        raw += rgba[y * row:(y + 1) * row]
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def ico_bytes(images):
    n = len(images)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    blob = b""
    offset = 6 + 16 * n
    for size, png in images:
        b = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(png), offset)
        blob += png
        offset += len(png)
    return header + entries + blob


def main():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    images = [(s, png_bytes(s, render(s))) for s in (256, 48, 32, 16)]
    with open(out_path, "wb") as f:
        f.write(ico_bytes(images))
    print(f"Wrote {out_path} ({os.path.getsize(out_path)} bytes)")


if __name__ == "__main__":
    main()
