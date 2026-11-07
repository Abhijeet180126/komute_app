"""Photo uploads (ISSUES.md H2), shared by the website and the API.
Moved unchanged from komute/views.py (still importable there as _resize_image)."""
import io
import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

upload_logger = logging.getLogger("komute.uploads")


def resize_image(file_obj, max_dim=1200, quality=85, label="image"):
    """Validate that file_obj is a decodable image under the size cap, and
    return a ContentFile with it resized to max_dim on its longest side.
    Raises django.core.exceptions.ValidationError if the upload is too large
    or isn't a valid image (including Pillow's decompression-bomb guard
    tripping) — callers must reject the upload, never fall back to storing
    the unverified raw bytes. Every upload is logged under `label`
    (ISSUES.md H2)."""
    max_bytes = settings.IMAGE_UPLOAD_MAX_MB * 1024 * 1024
    desc = (f"{label} name={getattr(file_obj, 'name', '')!r} size={file_obj.size} "
            f"type={getattr(file_obj, 'content_type', '')!r}")
    if file_obj.size > max_bytes:
        upload_logger.warning("upload rejected (too large): %s", desc)
        raise ValidationError(f"File is too large (max {settings.IMAGE_UPLOAD_MAX_MB} MB).")

    from PIL import Image, ImageOps
    try:
        file_obj.seek(0)
        img = Image.open(file_obj)
        original_dims = img.size
        # JPEGs decode at a reduced scale (no smaller than max_dim), which
        # keeps memory down for high-resolution phone photos.
        img.draft('RGB', (max_dim, max_dim))
        img = ImageOps.exif_transpose(img)  # fix camera rotation
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
    except Exception as e:
        upload_logger.warning("upload rejected (not a readable image: %s): %s", type(e).__name__, desc)
        raise ValidationError("That file isn't a valid image.")
    upload_logger.info("upload accepted: %s dims=%dx%d -> %dx%d, %d bytes",
                       desc, *original_dims, *img.size, buf.tell())
    return ContentFile(buf.getvalue(), name='image.jpg')
