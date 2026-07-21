"""Embed/extract a ZDoom savegame inside a PNG's own metadata.

The screenshot Paint writes and the game's resumable save-state travel as one
file: the raw .zds bytes ZDoom produces for `save`/`load` (see
DoomEngine.save_state / load_state) are base64'd into a zTXt chunk under a
private keyword. Every PNG viewer -- including Paint itself -- silently
ignores an unrecognized text chunk; only this module reads it back. That
keeps the "as far as Paint knows, you painted it" premise intact even for the
load path: the file that resumes your game is still a completely normal,
valid, viewable screenshot.
"""
import base64

from PIL import Image
from PIL.PngImagePlugin import PngInfo

# tEXt/zTXt keywords must be Latin-1 and <=79 bytes; well within that here.
_KEYWORD = "DoomPaintSave"


def embed_save(png_path: str, zds_bytes: bytes) -> None:
    """Rewrite the PNG at png_path in place, adding zds_bytes as metadata."""
    with Image.open(png_path) as img:
        img.load()  # pull in pixel data (and any trailing chunks) before rewriting
        text = dict(getattr(img, "text", {}))
        img2 = img.copy()
    info = PngInfo()
    for key, value in text.items():
        if key != _KEYWORD:
            info.add_text(key, value)
    info.add_text(_KEYWORD, base64.b64encode(zds_bytes).decode("ascii"), zip=True)
    img2.save(png_path, pnginfo=info)


def extract_save(png_path: str) -> "bytes | None":
    """Return the embedded .zds bytes, or None if this PNG has none."""
    with Image.open(png_path) as img:
        img.load()  # trailing text chunks aren't parsed until the image loads
        b64 = getattr(img, "text", {}).get(_KEYWORD)
    if not b64:
        return None
    return base64.b64decode(b64)
