"""Turning the club's artwork into home-screen icons.

Two things here have judgement in them and both fail quietly. Corners left
rounded in the picture show as white slivers once the phone rounds them again,
and a mark that reaches past the safe circle has its ends cropped off by a
round launcher — neither of which looks like a bug, only like a worse icon.

The pixel work is tested without `sips`; only the end-to-end run needs it, and
that is skipped where it is absent, so the suite passes on the machine that
serves the site as well as on the Mac that makes the icons.
"""

from __future__ import annotations

import math
import shutil

import pytest

from scripts.make_icons import (
    MARK,
    SAFE_ZONE,
    Unusable,
    content_box,
    main,
    padded,
    padding_needed,
    pixel,
    read_png,
    square_the_corners,
    tile_colour,
    write_png,
)

BLACK = (8, 8, 8)
WHITE = (255, 255, 255)
BURGUNDY = (111, 23, 43)


def picture(side, background):
    """A flat square of one colour, as rows of RGB triples."""
    return bytearray(bytes(background) * side * side)


def put(pixels, side, x, y, colour):
    at = (y * side + x) * 3
    pixels[at:at + 3] = bytes(colour)


def rounded_tile(side=200, radius=40, tile=BLACK, outside=WHITE):
    """A tile with rounded corners on a background, the way artwork arrives."""
    pixels = picture(side, tile)
    for y in range(side):
        for x in range(side):
            cx = min(x, side - 1 - x)
            cy = min(y, side - 1 - y)
            if cx < radius and cy < radius:
                if math.hypot(radius - cx, radius - cy) > radius:
                    put(pixels, side, x, y, outside)
    return pixels


# -- the PNG codec, since this file brought its own -------------------------


def test_a_picture_survives_being_written_and_read(tmp_path):
    pixels = rounded_tile(64, 12)
    write_png(tmp_path / "t.png", 64, 64, pixels)
    width, height, back = read_png(tmp_path / "t.png")
    assert (width, height) == (64, 64)
    assert back == pixels, "what comes back has to be what went in, pixel for pixel"


def test_something_that_is_not_a_png_says_so(tmp_path):
    (tmp_path / "t.png").write_bytes(b"not a png at all")
    with pytest.raises(Unusable, match="not a PNG"):
        read_png(tmp_path / "t.png")


# -- squaring the corners ---------------------------------------------------


def test_the_rounded_corners_are_filled_in(tmp_path):
    pixels = rounded_tile()
    assert pixel(pixels, 200, 0, 0) == WHITE, "the artwork starts with white corners"

    square_the_corners(pixels, 200, 200, BLACK)

    for x, y in ((0, 0), (199, 0), (0, 199), (199, 199), (3, 3), (196, 196)):
        assert pixel(pixels, 200, x, y) == BLACK, f"({x}, {y}) is still not the tile"


def test_it_fills_with_the_artworks_own_colour(tmp_path):
    """Not black. If Robert sends a burgundy version this still has to work,
    and a hard-coded colour would put a black frame round it."""
    pixels = rounded_tile(tile=BURGUNDY)
    found = tile_colour(pixels, 200, 200)
    assert found == BURGUNDY

    square_the_corners(pixels, 200, 200, found)
    assert pixel(pixels, 200, 0, 0) == BURGUNDY


def test_it_cannot_eat_the_mark(tmp_path):
    """The failure that would matter: the mark is white, like the corners, and
    a fill that reached it would paint out the design and leave a black square
    with nothing to show anything was wrong."""
    pixels = rounded_tile()
    for x in range(90, 110):
        put(pixels, 200, x, 100, WHITE)          # a white stroke in the middle

    square_the_corners(pixels, 200, 200, BLACK)

    assert pixel(pixels, 200, 100, 100) == WHITE, "the mark is still there"
    assert pixel(pixels, 200, 0, 0) == BLACK, "and the corners are still done"


def test_a_fill_that_escapes_is_refused(tmp_path):
    """A picture with no flat border lets the fill loose over everything. It
    would come out a solid square, so it is refused rather than written."""
    pixels = picture(100, WHITE)
    with pytest.raises(Unusable, match="escaped the tile"):
        square_the_corners(pixels, 100, 100, BLACK)


def test_an_already_square_picture_is_left_alone(tmp_path):
    pixels = picture(50, BLACK)
    assert square_the_corners(pixels, 50, 50, BLACK) == 0
    assert pixels == picture(50, BLACK)


# -- room for the mark ------------------------------------------------------


def with_mark(side, left, top, right, bottom):
    """A tile with a block of white where the mark would be."""
    pixels = picture(side, BLACK)
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            put(pixels, side, x, y, WHITE)
    return pixels


def test_the_mark_is_found_where_it_is(tmp_path):
    pixels = with_mark(200, 40, 80, 160, 120)
    assert content_box(pixels, 200, 200, BLACK) == (40, 80, 160, 120)


def test_a_flat_picture_has_no_mark_to_measure(tmp_path):
    with pytest.raises(Unusable, match="no mark in it"):
        content_box(picture(40, BLACK), 40, 40, BLACK)


def fits_the_safe_circle(side, box):
    """Whether every corner of the mark is inside the circle a launcher keeps."""
    left, top, right, bottom = box
    middle = side / 2
    reach = max(math.hypot(x - middle, y - middle)
                for x in (left, right) for y in (top, bottom))
    return reach <= SAFE_ZONE / 2 * side


def test_a_wide_mark_is_padded_until_it_fits(tmp_path):
    """FC Vino's own shape: wide, short, and reaching nearly edge to edge."""
    box = (27, 60, 173, 140)                      # 73% of a 200px tile across
    assert not fits_the_safe_circle(200, box), "it does not fit at full bleed"

    side = padding_needed(200, 200, box)
    assert side > 200
    shifted = (side - 200) // 2
    assert fits_the_safe_circle(
        side, (box[0] + shifted, box[1] + shifted, box[2] + shifted, box[3] + shifted))


def test_a_mark_with_room_already_is_not_padded(tmp_path):
    """Padding an icon that does not need it just makes it smaller on screen."""
    assert padding_needed(200, 200, (70, 70, 130, 130)) == 200


def test_padding_keeps_the_mark_in_the_middle(tmp_path):
    pixels = with_mark(100, 40, 40, 60, 60)
    out = padded(pixels, 100, 100, 160, BLACK)
    assert len(out) == 160 * 160 * 3
    assert pixel(out, 160, 0, 0) == BLACK, "the new room is the tile's colour"
    assert content_box(out, 160, 160, BLACK) == (70, 70, 90, 90), "centred"


# -- the whole run ----------------------------------------------------------

needs_sips = pytest.mark.skipif(shutil.which("sips") is None,
                                reason="sips is a macOS tool; the pixel work is tested above")


def artwork(side=200):
    """A rounded tile with something on it — a tile alone has no mark to measure."""
    pixels = rounded_tile(side)
    for y in range(side // 2 - 10, side // 2 + 10):
        for x in range(side // 4, side - side // 4):
            put(pixels, side, x, y, WHITE)
    return pixels


@needs_sips
def test_it_writes_the_three_icons_a_phone_asks_for(tmp_path):
    write_png(tmp_path / "art.png", 200, 200, artwork())
    assert main([str(tmp_path / "art.png"), "--into", str(tmp_path / "out")]) == 0

    for size in (180, 192, 512):
        made = tmp_path / "out" / f"icon-{size}.png"
        assert made.exists(), f"icon-{size}.png was not written"
        width, height, pixels = read_png(made)
        assert (width, height) == (size, size)
        assert max(pixel(pixels, width, 0, 0)) < 40, "and its corners are the tile"


@needs_sips
def test_artwork_that_is_not_square_is_refused(tmp_path, capsys):
    write_png(tmp_path / "wide.png", 120, 60, picture(1, BLACK) * 7200)
    assert main([str(tmp_path / "wide.png"), "--into", str(tmp_path / "out")]) == 2
    assert "cropping somebody's artwork" in capsys.readouterr().err


def test_missing_artwork_is_refused(tmp_path, capsys):
    assert main([str(tmp_path / "nothing.png"), "--into", str(tmp_path)]) == 2
    assert "no artwork at" in capsys.readouterr().err


def test_the_two_thresholds_are_not_the_same_number():
    """They were once, and tightening the fill made every JPEG-noisy corner
    count as part of the mark — which padded the icon by 75% to make room for
    nothing at all. They ask opposite questions of the same scale."""
    from scripts.make_icons import TOLERANCE

    assert TOLERANCE < MARK
