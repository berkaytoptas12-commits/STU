"""The app's file icon: the launcher's microchip as a Windows .ico (Explorer, taskbar, title bar, notifications).

Written at build time (packaging/techrag.spec) from the same drawing as the launcher icon - nothing is downloaded and
no image file is kept in the repository. Every size is drawn at its own pixel size; 16-48 px are stored as 32-bit
DIBs (read by every Windows version and API), 64 px and up as PNG.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def chip_rgba(size: int) -> np.ndarray:
    """size x size x 4 straight-alpha RGBA (top-down) of the chip; no shadow below 48 px, where it only blurs."""
    from techrag.launcher import render_icon

    bgra = np.frombuffer(render_icon(size, shadow=size >= 48, click_target=False), np.uint8)
    bgra = bgra.reshape(size, size, 4).astype(np.float32)
    a = bgra[..., 3:4]
    rgb = np.where(a > 0, np.clip(np.round(bgra[..., :3] * 255.0 / np.maximum(a, 1)), 0, 255), 0)   # un-premultiply
    return np.concatenate([rgb[..., ::-1], a], axis=2).astype(np.uint8)


def _png(rgba: np.ndarray) -> bytes:
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _dib(rgba: np.ndarray) -> bytes:
    """BITMAPINFOHEADER + bottom-up BGRA + an all-zero AND mask (the alpha channel is the transparency)."""
    h, w = rgba.shape[:2]
    header = struct.pack("<IiiHHIIiiII", 40, w, 2 * h, 1, 32, 0, 0, 0, 0, 0, 0)
    bgra = rgba[::-1, :, [2, 1, 0, 3]].tobytes()
    mask_row = ((w + 31) // 32) * 4
    return header + bgra + b"\x00" * (mask_row * h)


def ico_bytes(sizes=SIZES) -> bytes:
    images = [(s, (_png if s >= 64 else _dib)(chip_rgba(s))) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in images:
        dim = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def write_ico(path: str | Path, sizes=SIZES) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ico_bytes(sizes))
    return path
