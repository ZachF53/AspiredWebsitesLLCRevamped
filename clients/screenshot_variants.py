"""
Smaller copies of case-study screenshots for responsive `srcset`.

Desktop screenshots are captured at 1200 px wide, but a portfolio card
is roughly 465 px wide, so a phone downloads about 2.6x the pixels it
shows. A 600 px copy sits next to each original ("x.webp" ->
"x-600w.webp"); the card offers both and the browser picks.

The srcset is only emitted when the copy exists on disk, so a missing
variant falls back to the single full-size image instead of a broken one.
"""
import io
import posixpath

from django.core.files.base import ContentFile

VARIANT_WIDTH = 600
WEBP_QUALITY = 82


def variant_name(name, width=VARIANT_WIDTH):
    root, ext = posixpath.splitext(name)
    return f'{root}-{width}w{ext or ".webp"}'


def make_variant(field_file, width=VARIANT_WIDTH, force=False):
    """Write the resized copy next to the original. Returns True if written."""
    from PIL import Image

    storage = field_file.storage
    target = variant_name(field_file.name, width)
    if storage.exists(target) and not force:
        return False
    with storage.open(field_file.name, 'rb') as fh:
        img = Image.open(fh)
        img.load()
    if img.width <= width:
        return False
    img = img.convert('RGB')
    img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'WEBP', quality=WEBP_QUALITY, method=6)
    if storage.exists(target):
        storage.delete(target)
    storage.save(target, ContentFile(buf.getvalue()))
    return True


def srcset(field_file, full_width, width=VARIANT_WIDTH):
    """'small.webp 600w, full.webp 1200w', or '' when there is no copy."""
    if not field_file:
        return ''
    target = variant_name(field_file.name, width)
    try:
        if not field_file.storage.exists(target):
            return ''
    except Exception:  # noqa: BLE001 (a storage hiccup must not 500 the portfolio)
        return ''
    return f'{field_file.storage.url(target)} {width}w, {field_file.url} {full_width}w'
