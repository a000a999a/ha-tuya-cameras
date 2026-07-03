"""HTTP client for the AI detection inference service."""

from __future__ import annotations

import asyncio
import base64
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)
TIMEOUT = 5


class AIClient:
    """Thin async wrapper around the /analyze and /health endpoints."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/health",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def analyze(self, image_bytes: bytes) -> dict | None:
        """
        POST image to /analyze.
        Returns {human, confidence, annotated_image (bytes)} or None on any error.
        """
        try:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._url}/analyze",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("AI service returned HTTP %d", resp.status)
                        return None
                    result = await resp.json()
                    b64 = result.pop("annotated_image_b64", None)
                    if b64:
                        result["annotated_image"] = base64.b64decode(b64)
                    result.setdefault("animals", [])
                    return result
        except asyncio.TimeoutError:
            _LOGGER.warning("AI service timeout after %ds — will fail-open", TIMEOUT)
            return None
        except Exception as err:
            _LOGGER.warning("AI service error: %s — will fail-open", err)
            return None
