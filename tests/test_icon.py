"""The app's file icon: the launcher's microchip written as a valid Windows .ico at build time."""

import struct
import zlib

from techrag.icon import SIZES, chip_rgba, ico_bytes, write_ico


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


def test_the_icon_is_the_chip_with_transparent_corners():
    for s in (16, 32, 256):
        rgba = chip_rgba(s)
        assert rgba.shape == (s, s, 4)
        assert rgba[0, 0, 3] == 0 and rgba[-1, -1, 3] == 0, "transparent corners, no click-target alpha"
        c = rgba[s // 2, s // 2]
        assert c[3] == 255 and c[2] > c[0] + 60, "an opaque blue package in the middle"
