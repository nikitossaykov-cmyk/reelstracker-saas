"""Multi-photo collage for Flux Kontext input.

When the user uploads 2-4 angles of the same bottle, we paste them into
a single grid image and feed that to Flux Kontext as the `input_image`.
The model effectively gets multi-view conditioning out of an API that
formally only accepts one reference — labels, proportions and 3D shape
all reconstruct more reliably than from a single front-on shot.

Layout:
  1 photo  → straight pass-through (re-encoded JPEG, rotated by EXIF)
  2 photos → 1×2 horizontal strip (each tile 1024×1024)
  3 photos → top-left + top-right + bottom (centered)
  4 photos → 2×2 grid

Each tile is centre-fit (preserve aspect, pad black) so a portrait or
landscape source doesn't get distorted.

HEIC / HEIF support is registered at module load via pillow_heif so
iPhone uploads decode transparently. EXIF orientation is applied
before tiling so iPhone landscape-held shots don't end up sideways
in the collage.
"""
from __future__ import annotations

import io
from typing import Sequence

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001 — optional at import time
    pass


TILE_PX = 1024  # each cell is 1024×1024 — gives Flux a generous res per view


def _open_image(blob: bytes):
    """Decode any supported format (incl. HEIC/HEIF) and apply EXIF
    orientation. Returns a PIL RGB image right-side-up."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(blob))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _fit_tile(img, tile_size: int):
    """Aspect-preserve resize the source into a tile_size×tile_size square,
    padded with black."""
    from PIL import Image

    w, h = img.size
    scale = min(tile_size / w, tile_size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (tile_size, tile_size), (0, 0, 0))
    canvas.paste(resized, ((tile_size - nw) // 2, (tile_size - nh) // 2))
    return canvas


def build_collage(
    image_bytes_list: Sequence[bytes],
    *,
    tile_size: int = TILE_PX,
    jpeg_quality: int = 92,
) -> bytes:
    """Compose 1..4 source images into a single JPEG."""
    from PIL import Image

    n = len(image_bytes_list)
    if n == 0:
        raise ValueError("build_collage: no images")
    if n > 4:
        raise ValueError("build_collage: max 4 images, got %d" % n)

    # Single image — pass through but normalise to JPEG/RGB (and decode
    # HEIC + apply EXIF rotation) so downstream data-URI handling is
    # uniform.
    if n == 1:
        src = _open_image(image_bytes_list[0])
        buf = io.BytesIO()
        src.save(buf, format="JPEG", quality=jpeg_quality)
        return buf.getvalue()

    tiles = [_fit_tile(_open_image(b), tile_size) for b in image_bytes_list]

    if n == 2:
        canvas = Image.new("RGB", (tile_size * 2, tile_size), (0, 0, 0))
        canvas.paste(tiles[0], (0, 0))
        canvas.paste(tiles[1], (tile_size, 0))
    elif n == 3:
        canvas = Image.new("RGB", (tile_size * 2, tile_size * 2), (0, 0, 0))
        canvas.paste(tiles[0], (0, 0))
        canvas.paste(tiles[1], (tile_size, 0))
        # bottom: centered
        canvas.paste(tiles[2], (tile_size // 2, tile_size))
    else:  # n == 4
        canvas = Image.new("RGB", (tile_size * 2, tile_size * 2), (0, 0, 0))
        canvas.paste(tiles[0], (0, 0))
        canvas.paste(tiles[1], (tile_size, 0))
        canvas.paste(tiles[2], (0, tile_size))
        canvas.paste(tiles[3], (tile_size, tile_size))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()
