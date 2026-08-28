"""Send HTML emails (with optional inline image) via HA's native SMTP integration.

Replaces the old custom Notifier/smtplib class. Stateless — SMTP credentials
now live once, centrally, in HA's own SMTP integration (Settings → Devices &
Services → SMTP), not per config entry. Callers just need a list of
`notify.*` entity IDs (from that integration) to target.

Uses `smtp.send_message`, not the generic `notify.send_message` — only the
SMTP-specific action has an `html` field and attachment/inline-image support
(confirmed: the generic action only has plain-text `message`, no `html` at
all — HTML tags render literally if sent through it instead).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_MEDIA_SUBDIR      = "tuya_cameras"
_RETENTION_SECONDS = 7 * 86400  # matches the existing debug_snapshots retention


def _local_media_root(hass: HomeAssistant) -> Path:
    """Root directory backing media-source://media_source/local/...

    NOT the same as hass.config.path("media") in a Docker deployment: HA core's
    is_docker_env() check makes the "local" media dir default to /media instead
    of <config>/media (see homeassistant/core_config.py). Writing to path("media")
    and reading back via media-source silently targets two different directories
    in that case — read hass.config.media_dirs directly so write and read always
    agree, whatever the environment.
    """
    return Path(hass.config.media_dirs.get("local", hass.config.path("media")))


def _write_image_and_cleanup(hass: HomeAssistant, image_bytes: bytes) -> Path | None:
    """Write one motion image under the local media-source root, prune anything
    older than the retention window. Runs in the executor — blocking file I/O.
    """
    try:
        media_root = _local_media_root(hass) / _MEDIA_SUBDIR
        media_root.mkdir(parents=True, exist_ok=True)

        cutoff = time.time() - _RETENTION_SECONDS
        for old in media_root.glob("motion_*.jpg"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass  # best-effort cleanup, never block a send over one bad file

        filename = f"motion_{int(time.time() * 1000)}.jpg"
        path = media_root / filename
        path.write_bytes(image_bytes)
        return path
    except OSError as err:
        _LOGGER.error("Failed to write motion image for email attachment: %s", err)
        return None


async def send_email(
    hass: HomeAssistant,
    subject: str,
    html_body: str,
    notify_entities: list[str],
    image_bytes: bytes | None = None,
) -> None:
    """Send subject/html_body to every entity in notify_entities, with an
    optional inline image (referenced in html_body as <img src="cid:motion_image">).
    Never raises — logs and returns on any failure, matching the old Notifier's
    fail-open behaviour (a bad email must never break the motion pipeline).

    Each recipient is sent as its own independent smtp.send_message call, not one
    call with multiple targets. Confirmed live 2026-08-28: HA's entity-targeted
    fan-out for smtp.send_message processes multiple targets sequentially within a
    single service call, and a connection drop partway through silently skips the
    remaining, untried targets — on a real 4-recipient alert, 2 never even got an
    attempt logged, with only one generic error for the whole batch. Sending each
    recipient independently means one failure can't take out the others, and each
    gets its own clearly-attributed success/failure log line.
    """
    if not notify_entities:
        return

    service_data: dict = {"title": subject, "message": subject, "html": html_body}

    if image_bytes:
        path = await hass.async_add_executor_job(_write_image_and_cleanup, hass, image_bytes)
        if path:
            rel_path = path.relative_to(_local_media_root(hass))
            service_data["attachments"] = [{
                "media_source": {
                    "media_content_id": f"media-source://media_source/local/{rel_path.as_posix()}",
                    "media_content_type": "image/jpeg",
                },
                "filename":   path.name,
                "content_id": "motion_image",
            }]

    async def _send_one(entity_id: str) -> None:
        try:
            await hass.services.async_call(
                "smtp", "send_message", service_data,
                target={"entity_id": entity_id},
                blocking=True,
            )
            _LOGGER.debug("Email sent to %s: %s", entity_id, subject)
        except Exception as err:  # noqa: BLE001 — a failed send must never break the caller
            _LOGGER.error("Email send failed to %s: %s", entity_id, err)

    await asyncio.gather(*(_send_one(entity_id) for entity_id in notify_entities))
