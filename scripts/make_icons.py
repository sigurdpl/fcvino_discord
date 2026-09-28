#!/usr/bin/env python3
"""Turn the club's icon artwork into the three PNGs a phone wants.

    python scripts/make_icons.py data/fcvino_icon.jpeg

Writes `web/static/icon-180.png`, `icon-192.png` and `icon-512.png`, which is
what `web/routes/installable.py` looks for when it builds the manifest, and
what `web/deps.py:apple_icon` links for iOS.

**Why this is a script and not something done once by hand.** The icons are the
club's artwork, and `.gitignore` keeps that out of a public repository — so
every machine that serves the site needs them made or copied, and they have to
be remade whenever the artwork changes. This is the answer to both.

Two things it does to the artwork, neither of them a change to the design:

**It squares the corners.** Artwork usually arrives as a rounded tile on white,
because that is what an app icon looks like. But iOS masks an `apple-touch-icon`
with its own squircle and Android shapes an adaptive icon with its own outline,
so a radius baked into the picture does not line up with theirs — what shows is
white slivers along the corner arcs on iOS and four white corners on Android.
Hand the platform a full square and it rounds the corners once, itself.

**It pads the maskable sizes.** An Android adaptive icon may be cropped to a
circle 80% of the icon's width, so anything further out than that can be cut
off. A wide wordmark that fills its tile does not survive it. How much padding
is worked out from the artwork rather than assumed, and printed.

`sips` does the pixel-pushing it is good at — decoding the JPEG and resampling
— and this does the two things it cannot. That makes this a macOS tool. It is
not needed on the machine that serves the site: copy the three PNGs there, as
the wordmark and the photographs are already copied.
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "web" / "static"

# iOS reads the first from the page and never crops it, only rounds it. The
# other two are the manifest's, and are declared maskable as well as plain.
FULL_BLEED = (180,)
PADDED = (192, 512)

# The share of the icon's width an Android launcher is guaranteed not to crop,
# as a circle about the centre. Everything that matters has to be inside it.
SAFE_ZONE = 0.8

# How far a pixel may be from the tile's colour and still count as part of it —
# which is where the corner fill stops. Tight, because the edge of a rounded
# tile is anti-aliased and a generous value stops partway down that gradient,
# leaving the grey ghost of the old corner behind. A flat area of JPEG varies
# by two or three, so this has room to spare.
TOLERANCE = 16

# And how far it must be to count as part of the *mark* when measuring how much
# room the mark needs. The opposite job, so the opposite value: a flat tile is
# never quite flat in a JPEG, and measuring with the tight number above found
# "content" in all four corners and padded the icon by 75% to make room for it.
MARK = 60

# A fill that escapes the corners and reaches the mark would paint it out, and
# the picture would be a black square with nobody the wiser. The corners of a
# rounded tile are a few per cent; anything like this much has got loose.
RUNAWAY = 0.25


class Unusable(Exception):
    """Something about the artwork or this machine that a sentence can explain."""


# -- PNG, in and out --------------------------------------------------------
#
# Small enough to do here, and doing it here means no Pillow: this script runs
# on a laptop that has the club's artwork on it, not on a build server, and
# asking somebody to install an imaging library to make an icon is a good way
# for the icon never to be made.


def read_png(path: Path) -> tuple[int, int, bytearray]:
    """An 8-bit RGB or RGBA PNG as (width, height, rows of RGB triples)."""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise Unusable(f"{path} is not a PNG")
    pos, idat, header = 8, b"", None
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        kind, body = raw[pos + 4:pos + 8], raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length

    width, height, depth, colour, *_ = header
    if depth != 8 or colour not in (2, 6):
        raise Unusable("only 8-bit RGB or RGBA is handled here")
    step = 3 if colour == 2 else 4

    data = zlib.decompress(idat)
    stride = width * step
    pixels, previous, at = bytearray(), bytearray(stride), 0
    for _ in range(height):
        kind, line = data[at], bytearray(data[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - step] if i >= step else 0
            up = previous[i]
            upleft = previous[i - step] if i >= step else 0
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                pa, pb, pc = abs(up - upleft), abs(left - upleft), abs(left + up - 2 * upleft)
                best = left if pa <= pb and pa <= pc else up if pb <= pc else upleft
                line[i] = (line[i] + best) & 255
        previous = line
        if step == 3:
            pixels += line
        else:
            del line[3::4]          # the alpha channel, which icons do not use
            pixels += line
    return width, height, pixels


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    """Rows of RGB triples as an 8-bit RGB PNG, every row stored unfiltered."""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# -- the two things sips cannot do ------------------------------------------


def pixel(pixels: bytearray, width: int, x: int, y: int) -> tuple[int, int, int]:
    at = (y * width + x) * 3
    return pixels[at], pixels[at + 1], pixels[at + 2]


def near(a: tuple[int, int, int], b: tuple[int, int, int], tolerance: int = TOLERANCE) -> bool:
    return all(abs(p - q) <= tolerance for p, q in zip(a, b, strict=True))


def tile_colour(pixels: bytearray, width: int, height: int) -> tuple[int, int, int]:
    """The artwork's own background, read from the middle of its top edge.

    Not assumed to be black. That point is inside the tile — past the corner
    arc, above the wordmark — for any rounded-square icon, so the colour comes
    from the picture and a version of the artwork in another colour still works.
    """
    return pixel(pixels, width, width // 2, height // 20)


def square_the_corners(pixels: bytearray, width: int, height: int,
                       colour: tuple[int, int, int]) -> int:
    """Flood the four corners with the tile's colour. Returns pixels changed.

    A flood fill rather than drawing over a computed radius, because the radius
    is whatever the artist used and the fill finds it by itself. It cannot eat
    into the mark: white letters in the middle of the tile are not connected to
    a corner, since the tile's own colour surrounds them and stops it — unless
    the fill escapes the tile's edge entirely, which `RUNAWAY` is here to catch.
    """
    seen = bytearray(width * height)
    queue = deque((x, y) for x, y in
                  ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
                  if not near(pixel(pixels, width, x, y), colour))
    for x, y in queue:
        seen[y * width + x] = 1

    changed = 0
    while queue:
        x, y = queue.popleft()
        at = (y * width + x) * 3
        pixels[at], pixels[at + 1], pixels[at + 2] = colour
        changed += 1
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < width and 0 <= ny < height) or seen[ny * width + nx]:
                continue
            if near(pixel(pixels, width, nx, ny), colour):
                continue
            seen[ny * width + nx] = 1
            queue.append((nx, ny))

    if changed > RUNAWAY * width * height:
        raise Unusable(
            f"filling the corners spread over {changed / (width * height):.0%} of the "
            "picture, so it has escaped the tile rather than found its corners.\n"
            "That happens when the artwork has no flat border — pass it already "
            "square, or send it back for a version that has one."
        )
    return changed


def content_box(pixels: bytearray, width: int, height: int,
                colour: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """The bounds of everything that is not the tile: the mark itself.

    Judged with `MARK` rather than `TOLERANCE`: this asks what is clearly part
    of the design, where the corner fill asks what is clearly not part of the
    background, and the two want opposite ends of the same scale.
    """
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        row = y * width
        for x in range(width):
            at = (row + x) * 3
            if near((pixels[at], pixels[at + 1], pixels[at + 2]), colour, MARK):
                continue
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
    if right < 0:
        raise Unusable("the artwork is a single flat colour — there is no mark in it")
    return left, top, right, bottom


def padding_needed(width: int, height: int, box: tuple[int, int, int, int]) -> int:
    """The square side that puts the mark inside the safe circle, and centres it.

    A launcher may crop to a circle of `SAFE_ZONE` of the width, so what has to
    fit is the *corner* of the mark's box furthest from the middle — not its
    width. A wordmark is wide and short, and measuring the width alone pads far
    more than it needs to.
    """
    left, top, right, bottom = box
    cx, cy = width / 2, height / 2
    reach = max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                for x in (left, right) for y in (top, bottom))
    wanted = 2 * reach / SAFE_ZONE
    if wanted <= width:
        return width
    # Grown by the same whole number of pixels on all four sides, so the middle
    # of the picture stays the middle. An odd difference would leave `padded`
    # placing the mark half a pixel off centre, which pushes the far corner of
    # it back outside the circle this exists to fit it into.
    return width + 2 * math.ceil((wanted - width) / 2)


def padded(pixels: bytearray, width: int, height: int, side: int,
           colour: tuple[int, int, int]) -> bytearray:
    """The artwork centred on a larger square of its own colour."""
    out = bytearray(bytes(colour) * (side * side))
    left, top = (side - width) // 2, (side - height) // 2
    for y in range(height):
        at = (y * width) * 3
        into = ((top + y) * side + left) * 3
        out[into:into + width * 3] = pixels[at:at + width * 3]
    return out


# -- sips, for the parts it is good at --------------------------------------


def require_sips() -> None:
    if not shutil.which("sips"):
        raise Unusable(
            "`sips` is a macOS tool and this machine has not got it.\n"
            "Make the icons on the Mac that has the artwork and copy the three\n"
            "PNGs into web/static/ — which is what the serving machine needs anyway."
        )


def to_png(source: Path, target: Path) -> Path:
    subprocess.run(["sips", "-s", "format", "png", str(source), "--out", str(target)],
                   check=True, capture_output=True)
    return target


def resize(source: Path, target: Path, side: int) -> Path:
    subprocess.run(["sips", "-z", str(side), str(side), str(source), "--out", str(target)],
                   check=True, capture_output=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artwork", type=Path, help="a square picture of the icon")
    parser.add_argument("--into", type=Path, default=STATIC)
    args = parser.parse_args(argv)

    try:
        require_sips()
        if not args.artwork.exists():
            raise Unusable(f"no artwork at {args.artwork}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            width, height, pixels = read_png(to_png(args.artwork, tmp / "source.png"))
            if width != height:
                raise Unusable(
                    f"the artwork is {width}×{height}. An icon is square, and cropping "
                    "somebody's artwork is not this script's decision to make."
                )

            colour = tile_colour(pixels, width, height)
            print(f"artwork   {width}×{height}, tile colour #{bytes(colour).hex()}")

            squared = square_the_corners(pixels, width, height, colour)
            print(f"corners   {squared:,} pixels filled in "
                  f"({squared / (width * height):.1%} of it, the rounded corners)"
                  if squared else "corners   already square, nothing to fill")

            box = content_box(pixels, width, height, colour)
            side = padding_needed(width, height, box)
            print(f"mark      {box[2] - box[0] + 1}×{box[3] - box[1] + 1} at {box[:2]}")
            print(f"padding   {side}×{side} ({side / width - 1:+.0%}) so it sits inside "
                  f"the {SAFE_ZONE:.0%} circle a launcher may crop to")

            args.into.mkdir(parents=True, exist_ok=True)
            write_png(tmp / "full.png", width, height, pixels)
            write_png(tmp / "safe.png", side, side,
                      padded(pixels, width, height, side, colour))

            for size in FULL_BLEED:
                resize(tmp / "full.png", args.into / f"icon-{size}.png", size)
            for size in PADDED:
                resize(tmp / "safe.png", args.into / f"icon-{size}.png", size)

        made = [f"icon-{s}.png" for s in (*FULL_BLEED, *PADDED)]
        print(f"\nwrote {', '.join(made)} to {args.into}")
        print("These are the club's artwork, so they are not in the repository —\n"
              "copy them to whatever machine serves the site.")
        return 0
    except Unusable as exc:
        print(exc, file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"sips could not read {args.artwork}: "
              f"{exc.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
