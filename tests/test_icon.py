"""The app's icon: the shared bronze circuit-board disc (techrag/resources/sayisal-tasarim.png), decoded and scaled
without an image library, written as a valid Windows .ico at build time."""

import struct
import zlib

import numpy as np
import pytest

from techrag.config import resource_path
from techrag.icon import ICON_FILE, SIZES, decode_png, icon_rgba, ico_bytes, write_ico


def test_the_ico_holds_every_size_in_a_format_windows_reads(tmp_path):
    data = write_ico(tmp_path / "app.ico").read_bytes()
    assert data == ico_bytes(), "deterministic: drawn in code, nothing loaded from outside"
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(SIZES))
    seen = []
    for i in range(count):
        w, h, colors, _, planes, bpp, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i: 22 + 16 * i])
        px = w or 256
        seen.append(px)
        assert w == h and planes == 1 and bpp == 32 and colors == 0
        img = data[offset: offset + size]
        assert len(img) == size
        if px >= 64:
            assert img[:8] == b"\x89PNG\r\n\x1a\n"
            ihdr = struct.unpack(">IIBBBBB", img[16:29])
            assert ihdr[:4] == (px, px, 8, 6), "RGBA, 8 bit"
            assert zlib.crc32(img[12:29]) & 0xFFFFFFFF == struct.unpack(">I", img[29:33])[0]
        else:
            hdr = struct.unpack("<IiiHHIIiiII", img[:40])
            assert hdr[:5] == (40, px, 2 * px, 1, 32), "BITMAPINFOHEADER of a 32-bit icon (height doubled for the mask)"
            assert len(img) == 40 + px * px * 4 + ((px + 31) // 32) * 4 * px
    assert seen == list(SIZES)


def test_the_icon_is_the_bronze_disc_with_transparent_corners():
    for s in (16, 32, 256):
        rgba = icon_rgba(s)
        assert rgba.shape == (s, s, 4)
        assert rgba[0, 0, 3] == 0 and rgba[-1, -1, 3] == 0, "transparent corners"
        assert rgba[s // 2, s // 2, 3] == 255, "an opaque disc in the middle"
    big = icon_rgba(256).astype(int)
    opaque = big[big[..., 3] == 255]
    dark = opaque[opaque[..., :3].max(axis=1) < 70]
    bronze = opaque[(opaque[..., 0] > 150) & (opaque[..., 0] > opaque[..., 2] + 40)]
    assert len(dark) > len(bronze) > 0.1 * len(opaque), "bronze traces on a charcoal disc"


def test_the_full_size_icon_is_the_shipped_picture_unchanged():
    data = resource_path("resources", ICON_FILE).read_bytes()
    assert np.array_equal(icon_rgba(256), decode_png(data))


def _png_bytes(rgba: np.ndarray, filters) -> bytes:
    h, w, c = rgba.shape
    rows, prev = [], np.zeros(w * c, np.int32)
    for y in range(h):
        line = rgba[y].reshape(-1).astype(np.int32)
        f = filters[y % len(filters)]
        left = np.concatenate([np.zeros(c, np.int32), line[:-c]])
        if f == 0:
            enc = line
        elif f == 1:
            enc = line - left
        elif f == 2:
            enc = line - prev
        elif f == 3:
            enc = line - ((left + prev) >> 1)
        else:
            ul = np.concatenate([np.zeros(c, np.int32), prev[:-c]])
            pa, pb, pc = np.abs(prev - ul), np.abs(left - ul), np.abs(left + prev - 2 * ul)
            enc = line - np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prev, ul))
        rows.append(bytes([f]) + (enc & 0xFF).astype(np.uint8).tobytes())
        prev = line

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    color = 6 if c == 4 else 2
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


@pytest.mark.parametrize("channels", [4, 3])
def test_png_decoding_handles_every_filter(channels):
    rgba = np.random.default_rng(7).integers(0, 256, (9, 7, channels), dtype=np.uint8)
    out = decode_png(_png_bytes(rgba, filters=[0, 1, 2, 3, 4]))
    assert np.array_equal(out[..., :channels], rgba)
    if channels == 3:
        assert (out[..., 3] == 255).all()


def test_png_decoding_refuses_what_it_cannot_read():
    with pytest.raises(ValueError):
        decode_png(b"not a png")
