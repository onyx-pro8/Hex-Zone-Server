"""Prepare and persist profile avatars.

Catbox (and similar anonymous hosts) often block datacenter IPs with
``412 Invalid uploader``, which is why production uploads failed with 502.
Primary path: validate + compress with Pillow and store a data URL on the
owner row. Optional Catbox upload runs only when ``CATBOX_USERHASH`` is set
and succeeds; otherwise we keep the local data URL.

Avatars with a solid near-black background (typical logo uploads) are keyed
to transparency and stored/served as PNG so clients do not show a black disc.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from typing import Tuple

import httpx
from fastapi import HTTPException, status
from PIL import Image, UnidentifiedImageError

from app.core.config import settings

logger = logging.getLogger(__name__)

CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
MAX_INPUT_BYTES = 2_500_000
MAX_STORED_DATA_URL_CHARS = 320_000
MAX_AVATAR_EDGE_PX = 512
MAX_CHAT_EDGE_PX = 1280
MAX_CHAT_DATA_URL_CHARS = 700_000
JPEG_QUALITY = 78
CHAT_JPEG_QUALITY = 72
BLACK_BG_THRESHOLD = 36
BLACK_BG_SOFT = 58


def parse_image_payload(raw: str) -> Tuple[bytes, str]:
    """Accept a data URL or raw base64 string; return (bytes, mime)."""
    text = (raw or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image data is required",
        )

    mime = "image/jpeg"
    b64 = text
    match = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
        text,
        flags=re.DOTALL,
    )
    if match:
        mime = match.group(1).lower()
        b64 = match.group(2)

    if mime not in {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported image type. Use JPEG, PNG, WebP, or GIF.",
        )

    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image encoding",
        ) from exc

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image is empty",
        )
    if len(data) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image is too large (max about 2.5 MB)",
        )
    return data, "image/jpeg" if mime == "image/jpg" else mime


def _is_near_black(pixel: tuple[int, ...], *, threshold: int = BLACK_BG_THRESHOLD) -> bool:
    r, g, b = int(pixel[0]), int(pixel[1]), int(pixel[2])
    a = int(pixel[3]) if len(pixel) > 3 else 255
    if a < 8:
        return True
    return r <= threshold and g <= threshold and b <= threshold


def clear_solid_black_background(img: Image.Image) -> Image.Image:
    """If corners look like a solid black plate, key near-black pixels to alpha."""
    rgba = img.convert("RGBA")
    width, height = rgba.size
    if width < 2 or height < 2:
        return rgba

    corners = [
        rgba.getpixel((0, 0)),
        rgba.getpixel((width - 1, 0)),
        rgba.getpixel((0, height - 1)),
        rgba.getpixel((width - 1, height - 1)),
    ]
    if sum(1 for corner in corners if _is_near_black(corner)) < 3:
        return rgba

    pixels = rgba.load()
    soft = BLACK_BG_SOFT
    hard = BLACK_BG_THRESHOLD
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue
            brightest = r if r >= g else g
            if b > brightest:
                brightest = b
            if brightest <= hard:
                pixels[x, y] = (r, g, b, 0)
            elif brightest < soft:
                fade = int(a * (brightest - hard) / (soft - hard))
                pixels[x, y] = (r, g, b, fade)
    return rgba


def _encode_avatar_image(rgba: Image.Image, *, max_chars: int) -> str:
    """Prefer PNG when transparency exists; otherwise JPEG."""
    alpha = rgba.getchannel("A")
    has_transparency = alpha.getextrema()[0] < 250
    out = io.BytesIO()
    if has_transparency:
        rgba.save(out, format="PNG", optimize=True)
        mime = "image/png"
    else:
        rgba.convert("RGB").save(
            out, format="JPEG", quality=JPEG_QUALITY, optimize=True
        )
        mime = "image/jpeg"
    compressed = out.getvalue()
    data_url = f"data:{mime};base64," + base64.b64encode(compressed).decode("ascii")
    if len(data_url) > max_chars:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image is still too large after compression. Crop tighter and try again.",
        )
    return data_url


def prepare_avatar_image_bytes(data: bytes) -> tuple[bytes, str]:
    """Decode, clear solid black backgrounds, return (bytes, media_type)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgba = clear_solid_black_background(img)
            rgba.thumbnail((MAX_AVATAR_EDGE_PX, MAX_AVATAR_EDGE_PX), Image.Resampling.LANCZOS)
            alpha = rgba.getchannel("A")
            has_transparency = alpha.getextrema()[0] < 250
            out = io.BytesIO()
            if has_transparency:
                rgba.save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png"
            rgba.convert("RGB").save(
                out, format="JPEG", quality=JPEG_QUALITY, optimize=True
            )
            return out.getvalue(), "image/jpeg"
    except UnidentifiedImageError:
        return data, "image/jpeg"
    except Exception as exc:
        logger.warning("Avatar prepare failed: %s", exc)
        return data, "image/jpeg"


def compress_to_jpeg_data_url(
    data: bytes,
    *,
    max_edge_px: int,
    quality: int,
    max_chars: int,
) -> str:
    """Resize/compress to a JPEG data URL suitable for DB + mobile Image."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((max_edge_px, max_edge_px), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            compressed = out.getvalue()
    except UnidentifiedImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not decode image. Try another photo.",
        ) from exc
    except Exception as exc:
        logger.warning("Image compress failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not process image. Try another photo.",
        ) from exc

    data_url = "data:image/jpeg;base64," + base64.b64encode(compressed).decode("ascii")
    if len(data_url) > max_chars:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image is still too large after compression. Crop tighter and try again.",
        )
    return data_url


def compress_avatar_to_data_url(data: bytes) -> str:
    """Resize, clear solid black backgrounds, store PNG when alpha is needed."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgba = clear_solid_black_background(img)
            rgba.thumbnail((MAX_AVATAR_EDGE_PX, MAX_AVATAR_EDGE_PX), Image.Resampling.LANCZOS)
            return _encode_avatar_image(rgba, max_chars=MAX_STORED_DATA_URL_CHARS)
    except UnidentifiedImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not decode image. Try another photo.",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Avatar compress failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not process image. Try another photo.",
        ) from exc


async def _try_catbox_upload(
    image_bytes: bytes,
    userhash: str,
    *,
    filename: str,
    content_type: str,
) -> str | None:
    """Best-effort Catbox upload. Returns HTTPS URL or None on failure."""
    form: dict[str, str] = {"reqtype": "fileupload", "userhash": userhash}
    try:
        async with httpx.AsyncClient(
            timeout=45.0,
            headers={"User-Agent": "SafeZonePatrol/1.0 (+avatar-upload)"},
            follow_redirects=True,
        ) as client:
            response = await client.post(
                CATBOX_UPLOAD_URL,
                data=form,
                files={"fileToUpload": (filename, image_bytes, content_type)},
            )
    except httpx.HTTPError as exc:
        logger.warning("Catbox unreachable: %s", exc)
        return None

    body = (response.text or "").strip()
    if response.status_code >= 400:
        logger.warning(
            "Catbox rejected upload status=%s body=%s",
            response.status_code,
            body[:200],
        )
        return None

    # Accept http(s) and normalize to https for files.catbox.moe
    if body.startswith("http://"):
        body = "https://" + body[len("http://") :]
    if not body.startswith("https://"):
        logger.warning("Catbox returned non-URL body: %s", body[:200])
        return None
    return body


def _bytes_and_mime_from_data_url(data_url: str) -> tuple[bytes, str, str]:
    match = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
        data_url,
        flags=re.DOTALL,
    )
    if not match:
        raw = data_url.split(",", 1)[1]
        return base64.b64decode(raw), "image/jpeg", "avatar.jpg"
    mime = match.group(1).lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    data = base64.b64decode(match.group(2))
    filename = "avatar.png" if mime == "image/png" else "avatar.jpg"
    return data, mime, filename


async def _persist_compressed_image(data_url: str, *, log_label: str) -> str:
    userhash = (settings.CATBOX_USERHASH or "").strip()
    if userhash:
        image_bytes, content_type, filename = _bytes_and_mime_from_data_url(data_url)
        hosted = await _try_catbox_upload(
            image_bytes,
            userhash,
            filename=filename,
            content_type=content_type,
        )
        if hosted:
            logger.info("%s uploaded to Catbox: %s", log_label, hosted)
            return hosted
        logger.info("Catbox upload skipped/failed for %s; storing compressed data URL", log_label)
    return data_url


async def upload_avatar_image(raw_image: str) -> str:
    """Return a persistable avatar URL (hosted HTTPS or data URL)."""
    data, _mime = parse_image_payload(raw_image)
    data_url = compress_avatar_to_data_url(data)
    return await _persist_compressed_image(data_url, log_label="Avatar")


async def upload_chat_image(raw_image: str) -> str:
    """Compress a chat photo and return a data URL.

    Chat photos are painted by React Native ``Image``, which often fails to load
    third-party hosts (same reason avatars are proxied). Keep a compressed data
    URL so inbox cards render without a separate media fetch.
    """
    data, _mime = parse_image_payload(raw_image)
    return compress_to_jpeg_data_url(
        data,
        max_edge_px=MAX_CHAT_EDGE_PX,
        quality=CHAT_JPEG_QUALITY,
        max_chars=MAX_CHAT_DATA_URL_CHARS,
    )


def owner_has_avatar(avatar_url: str | None) -> bool:
    return bool(avatar_url and str(avatar_url).strip())


def client_avatar_url(base_url: str, owner_id: int, avatar_url: str | None) -> str | None:
    """Never embed data: or third-party host URLs in JSON profiles.

    Mobile clients fetch GET /owners/{id}/avatar with auth and cache the bytes.
    Returning catbox/https directly often fails to paint in React Native Image.
    """
    raw = (avatar_url or "").strip()
    if not raw:
        return None
    base = (base_url or "").rstrip("/")
    return f"{base}/owners/{int(owner_id)}/avatar"


def avatar_bytes_and_media_type(avatar_url: str | None) -> tuple[bytes, str] | None:
    """Decode a stored avatar into raw bytes for HTTP image responses.

    Solid black logo plates are keyed to transparency so existing stored JPEG
    avatars also render without a black disc.
    """
    raw = (avatar_url or "").strip()
    if not raw:
        return None
    if raw.startswith("data:"):
        match = re.match(
            r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
            raw,
            flags=re.DOTALL,
        )
        if not match:
            return None
        try:
            data = base64.b64decode(match.group(2), validate=False)
        except Exception:
            return None
        if not data:
            return None
        return prepare_avatar_image_bytes(data)
    return None
