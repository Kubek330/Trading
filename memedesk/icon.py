"""MemeDesk app icon, drawn in pure Python (no image files, no Pillow)."""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache

BG, HEAD, VISOR, GREEN = (5, 7, 10), (233, 238, 243), (11, 15, 20), (53, 208, 127)


def _color(x: float, y: float) -> tuple[int, int, int]:
    # eyes, visor, antenna ball, antenna stick, head: topmost layer first
    for cx in (0.42, 0.58):
        if ((x - cx) / 0.032) ** 2 + ((y - 0.555) / 0.045) ** 2 <= 1:
            return GREEN
    if 0.30 <= x <= 0.70 and 0.49 <= y <= 0.62:
        dx, dy = max(0.37 - x, 0, x - 0.63), max(0.555 - y, 0, y - 0.555)
        if dx * dx + dy * dy <= 0.07 ** 2:
            return VISOR
    if (x - 0.5) ** 2 + (y - 0.205) ** 2 <= 0.042 ** 2:
        return GREEN
    if abs(x - 0.5) <= 0.016 and 0.205 <= y <= 0.32:
        return HEAD
    if ((x - 0.5) / 0.33) ** 2 + ((y - 0.57) / 0.27) ** 2 <= 1:
        return HEAD
    return BG


@lru_cache(maxsize=4)
def png(size: int) -> bytes:
    ss = 2 if size <= 256 else 1  # light anti-aliasing for the small icon
    rows = []
    for j in range(size):
        row = bytearray(b"\x00")
        for i in range(size):
            r = g = b = 0
            for sj in range(ss):
                for si in range(ss):
                    c = _color((i + (si + 0.5) / ss) / size, (j + (sj + 0.5) / ss) / size)
                    r, g, b = r + c[0], g + c[1], b + c[2]
            n = ss * ss
            row += bytes((r // n, g // n, b // n))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


MANIFEST = {
    "name": "MemeDesk", "short_name": "MemeDesk", "start_url": "/#desk", "scope": "/",
    "display": "standalone", "orientation": "portrait",
    "background_color": "#05070a", "theme_color": "#05070a",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}
