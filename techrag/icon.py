"""The app's icon: the bronze circuit-board disc of the Sayısal Tasarım Uzmanı, shared with the department's
"Yapay Zeka Asistanları" launcher (techrag/resources/sayisal-tasarim.png, 256 x 256, transparent corners).

The same picture is the round launcher icon on the desktop (techrag.launcher.render_icon) and the exe's Windows .ico
(Explorer, taskbar, title bar, notifications), written at build time by packaging/techrag.spec. The PNG is decoded and
scaled here with numpy (area averaging in premultiplied alpha, so the edges get no dark fringe): nothing is
downloaded and no image library is needed. In the .ico, 16-48 px are stored as 32-bit DIBs (read by every Windows
version and API), 64 px and up as PNG.
"""

from __future__ import annotations

import struct
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICON_FILE = "sayisal-tasarim.png"


def decode_png(data: bytes) -> np.ndarray:
    """An 8-bit, non-interlaced RGBA or RGB PNG as an h x w x 4 uint8 array (straight alpha)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, header = 8, [], None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if header is None:
        raise ValueError("PNG without IHDR")
    w, h, depth, color, _, _, interlace = header
    if depth != 8 or color not in (2, 6) or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, color type {color}, interlace {interlace})")
    bpp = 4 if color == 6 else 3
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(h, 1 + w * bpp)
    out = np.zeros((h, w * bpp), np.int32)
    prev = np.zeros(w * bpp, np.int32)
    for y in range(h):
        kind, line = raw[y, 0], raw[y, 1:].astype(np.int32)
        if kind == 0:
            cur = line
        elif kind == 2:
            cur = (line + prev) & 0xFF
        else:   # 1 (sub), 3 (average), 4 (Paeth) depend on the pixel to the left: byte by byte
            cur = np.zeros_like(line)
            for i in range(len(line)):
                left = cur[i - bpp] if i >= bpp else 0
                if kind == 1:
                    pred = left
                elif kind == 3:
                    pred = (left + prev[i]) >> 1
                elif kind == 4:
                    up, ul = prev[i], (prev[i - bpp] if i >= bpp else 0)
                    pa, pb, pc = abs(up - ul), abs(left - ul), abs(left + up - 2 * ul)
                    pred = left if pa <= pb and pa <= pc else (up if pb <= pc else ul)
                else:
                    raise ValueError(f"bad PNG filter {kind}")
                cur[i] = (line[i] + pred) & 0xFF
        out[y], prev = cur, cur
    px = out.reshape(h, w, bpp).astype(np.uint8)
    if bpp == 3:
        px = np.concatenate([px, np.full((h, w, 1), 255, np.uint8)], axis=2)
    return px


@lru_cache(maxsize=1)
def _source() -> np.ndarray:
    """The shipped picture, premultiplied, 0..1 floats."""
    from techrag.config import resource_path

    rgba = decode_png(resource_path("resources", ICON_FILE).read_bytes()).astype(np.float64) / 255.0
    rgba[..., :3] *= rgba[..., 3:4]
    return rgba


def _area_weights(n_in: int, n_out: int) -> np.ndarray:
    """n_out x n_in: how much of each source pixel falls into each target pixel (rows sum to 1)."""
    edges = np.arange(n_out + 1) * (n_in / n_out)
    lo, hi = edges[:-1, None], edges[1:, None]
    src = np.arange(n_in)[None, :]
    overlap = np.clip(np.minimum(hi, src + 1) - np.maximum(lo, src), 0, None)
    return overlap / overlap.sum(axis=1, keepdims=True)


@lru_cache(maxsize=16)
def icon_premultiplied(size: int) -> np.ndarray:
    """size x size x 4 premultiplied RGBA floats (0..1) of the icon, scaled by area averaging."""
    src = _source()
    wy, wx = _area_weights(src.shape[0], size), _area_weights(src.shape[1], size)
    rows = np.tensordot(wy, src, axes=(1, 0))                     # size x w x 4
    out = np.ascontiguousarray(np.tensordot(rows, wx, axes=(1, 1)).transpose(0, 2, 1))   # size x size x 4
    out.setflags(write=False)
    return out


def icon_rgba(size: int) -> np.ndarray:
    """size x size x 4 straight-alpha RGBA uint8 (top-down) of the icon."""
    pm = icon_premultiplied(size)
    a = pm[..., 3:4]
    rgb = np.where(a > 0, pm[..., :3] / np.maximum(a, 1e-12), 0)
    return np.round(np.clip(np.concatenate([rgb, a], axis=2), 0, 1) * 255).astype(np.uint8)


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
    images = [(s, (_png if s >= 64 else _dib)(icon_rgba(s))) for s in sizes]
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
