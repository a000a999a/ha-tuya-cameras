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

from .recording_helper import _safe_slug

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


def _write_image_and_cleanup(hass: HomeAssistant, image_bytes: bytes, stem: str) -> Path | None:
    """Write one motion image as <stem>.jpg under the local media-source root, prune
    anything older than the retention window. Runs in the executor — blocking file I/O.

    stem is e.g. "Winterthur_Camera_Door_2026-11-10_15-20-05_2" (UTC, same naming as
    the recordings) so a media gallery — and the email attachment name — show which
    camera and when. The prune covers every top-level .jpg here, including the older
    motion_<epoch-ms>_<hex>.jpg names; recordings live in a subfolder and are untouched.
    """
    try:
        media_root = _local_media_root(hass) / _MEDIA_SUBDIR
        media_root.mkdir(parents=True, exist_ok=True)

        cutoff = time.time() - _RETENTION_SECONDS
        for old in media_root.glob("*.jpg"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass  # best-effort cleanup, never block a send over one bad file

        path = media_root / f"{stem}.jpg"
        n = 2
        while path.exists():  # two events on one camera within the same second
            path = media_root / f"{stem}-{n}.jpg"
            n += 1
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
    images: list[bytes] | bytes | None = None,
    image_name: str | None = None,
) -> None:
    """Send subject/html_body to every entity in notify_entities, with optional
    inline images: a single bytes is referenced as <img src="cid:motion_image">,
    a list as <img src="cid:motion_image_0">, <img src="cid:motion_image_1">, ….
    image_name (e.g. "Garden Front Camera") names the saved image files
    <image_name>_<UTC time>[_<n>].jpg; without it they are named motion_<UTC time>.
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

    if isinstance(images, bytes):
        image_list, indexed = [images], False
    else:
        image_list, indexed = list(images or []), True

    # "Garden " + " Front Camera" → "Garden_Front_Camera" (area names can carry stray spaces)
    slug = "_".join(part for part in _safe_slug(image_name or "motion").split("_") if part)
    base = f"{slug}_{time.strftime('%Y-%m-%d_%H-%M-%S', time.gmtime())}"
    attachments = []
    for i, img in enumerate(image_list):
        stem = f"{base}_{i + 1}" if len(image_list) > 1 else base
        path = await hass.async_add_executor_job(_write_image_and_cleanup, hass, img, stem)
        if not path:
            continue
        rel_path = path.relative_to(_local_media_root(hass))
        attachments.append({
            "media_source": {
                "media_content_id": f"media-source://media_source/local/{rel_path.as_posix()}",
                "media_content_type": "image/jpeg",
            },
            "filename":   path.name,
            "content_id": f"motion_image_{i}" if indexed else "motion_image",
        })
    if attachments:
        service_data["attachments"] = attachments

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
