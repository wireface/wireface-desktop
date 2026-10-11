"""New faces for the agent: a portrait of anyone or anything, drawn by wireface.dev's portrait service, for the face to
wear.

The agent's face tool (``set_face`` in ``backends/claude.py``) asks for one with who it should be, as the user said
it ("John Cena", "Vision", "a grizzled pirate captain"). The service (``site/portrait`` in the Wireface suite) frames it
like the built-in portraits (head and shoulders, straight on, mouth closed, plain background) and has FLUX.2 draw it
through OpenRouter: the bigger model when it names someone, the small one for a plain description. No key is needed
here. The face page then finds its landmarks (``web/face/photo.js``), as for
a photo dropped on the face, and the app wears it (``App.make_face``).
"""
from __future__ import annotations

import base64
import binascii
import logging

import aiohttp

from . import config
from .version import VERSION

log = logging.getLogger(__name__)

URL = config.PORTRAIT_URL
TIMEOUT_S = 150
MAX_DESCRIPTION = 300                         # what the service takes


class FaceMakerError(Exception):
    pass


def clean(description: str) -> str:
    who = " ".join(str(description or "").split())[:MAX_DESCRIPTION].strip()
    if not who:
        raise FaceMakerError("say who or what the face should be")
    return who


async def draw(who: str) -> tuple[bytes, str]:
    """A square portrait of ``who`` -> (image bytes, MIME type)."""
    body = {"description": clean(who)}
    headers = {"X-Wireface-Client": f"desktop/{VERSION}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
            async with s.post(URL, json=body, headers=headers) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:                   # not JSON: the proxy's own error page, say
                    data = None
                data = data if isinstance(data, dict) else {}
                status = r.status
    except (aiohttp.ClientError, TimeoutError) as e:
        raise FaceMakerError(f"can't reach wireface.dev to draw it: {e or type(e).__name__}") from None
    if status == 429 and not data.get("error"):
        raise FaceMakerError("too many new faces in a row: wait a minute and try again")
    if status >= 400:
        raise FaceMakerError(data.get("error") or f"wireface.dev answered {status}")
    try:
        image = base64.b64decode(data.get("image") or "", validate=True)
    except (binascii.Error, ValueError):
        image = b""
    if not image:
        raise FaceMakerError("wireface.dev sent no picture")
    log.info("drew a face (%s, %d KB)", data.get("model"), len(image) // 1024)
    return image, data.get("mimeType") or "image/jpeg"
