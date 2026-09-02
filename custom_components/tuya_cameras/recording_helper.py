"""Local motion recording (camera.record on confirmed human detection) and its
housekeeping (retention-based cleanup, low-disk-space alerting + pruning).

Added 2026-08-31. Applies to both the MQTT and ONVIF motion paths — callers invoke
start_recording() only after a human has already been confirmed by AI, deliberately
not on raw/unfiltered motion, so a false positive (wind, shadows, an animal) never
burns a full recording. Video is referenced by its saved path in the alert email,
never attached (large files, most mail clients don't render video inline).

Storage root: unlike notify_helper's motion-image attachments (which must live
under hass.config.media_dirs["local"] because they're referenced via a
media-source:// URI that HA resolves against that root), recordings are only
ever referenced by plain filesystem path in the alert email text — no
media-source resolution needed. So in principle any writable directory would
do — EXCEPT that HA's own camera.record service enforces its own security
allowlist independent of filesystem permissions: `stream.async_record()`
rejects any target path not under `hass.config.allowlist_external_dirs`.
Confirmed directly from HA core (core_config.py):

    hac.allowlist_external_dirs = {hass.config.path("www"), *hac.media_dirs.values()}

i.e. exactly two locations are allowed with zero extra configuration: the
media root (media_dirs["local"], which on a Docker install defaults to /media
and is typically NOT host-mounted — the original problem) and <config>/www/.
Recordings are therefore stored under hass.config.path("www", path_root) —
always allowlisted with no configuration.yaml edit needed, and still under
the already-persistent /config bind mount on every HA install type (Docker,
HAOS, Supervised, Core). Confirmed live 2026-09-01: writing directly under
/config (outside www/) raises `HomeAssistantError: Can't write ..., no access
to path!` even though the directory itself is writable.

Stream warm-up: confirmed live 2026-09-01 on two separate Tuya-cloud-relayed
cameras (Camera Door, Panorama Camera escadas) that calling camera.record
"cold" — with no viewer already watching that camera — frequently fails
partway through with `av.error.ArgumentError: ... non monotonically
increasing dts to muxer`. Root cause: HA's stream component caches at most
one Stream (decode worker) per camera, created via Camera.async_create_stream()
— both camera.record's handler and any live viewer share the exact same
cached Stream object (confirmed by reading homeassistant/components/camera/
__init__.py directly). A brand-new connection to Tuya's RTSP relay doesn't
guarantee a stable timestamp base from frame one; the muxer aborts outright
if it isn't. Starting a live view (e.g. opening the camera on the HA
dashboard) before triggering a recording reliably avoided the same failure
(confirmed working: the one fully successful recording so far, Winti
Terrasse via ONVIF, went through an already-live connection, not a cold
one). So start_recording() now pre-warms the same cached Stream itself —
calling camera.async_create_stream() and giving it a few seconds — before
calling the camera.record service, so the record call reuses an
already-stabilizing connection instead of cold-starting its own.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from homeassistant.components.camera import get_camera_from_entity_id
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Tech-alert + aggressive-prune threshold: below EITHER of these, housekeeping starts
# deleting the oldest remaining recordings regardless of retention, not just alerting
# and letting the disk actually fill toward the root filesystem.
LOW_DISK_GB      = 5
LOW_DISK_PERCENT = 5

# How long to let a freshly-opened stream stabilize before recording from it —
# see module docstring "Stream warm-up". Raised from 3s to 12s on 2026-09-02:
# 3s reduced but did not eliminate the non-monotonic-dts muxer failure on
# Tuya-cloud-relayed cameras — roughly half of real events still failed
# (confirmed via actual .mp4 vs orphaned .mp4.tmp files on disk, not just log
# lines). ONVIF-sourced recordings (a direct LAN connection, no cloud relay)
# have had zero failures regardless of warm-up duration, consistent with the
# relay path being the actual source of instability, not the local stream
# handling — a longer warm-up gives the relay connection more time to settle
# before the muxer starts trusting its timestamps.
STREAM_WARMUP_S = 12


def _recordings_root(hass: HomeAssistant, path_root: str) -> Path:
    """Root directory for a given recording_path config value, under /config/www —
    see the module docstring for why this differs from notify_helper's media root.
    """
    return Path(hass.config.path("www", path_root))


def _safe_slug(text: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in text).strip("_")
    return slug or "camera"


async def start_recording(
    hass: HomeAssistant,
    camera_entity_id: str,
    area: str,
    name: str,
    duration_s: int,
    path_root: str,
) -> str | None:
    """Kick off camera.record for duration_s seconds. Returns the saved file's
    absolute path (for referencing in the alert email), or None on failure.

    Pre-warms the camera's stream before recording (see module docstring
    "Stream warm-up") — this makes the call take STREAM_WARMUP_S seconds longer
    than before, so callers must NOT await this inline on the alert-email path;
    fire it as a background task instead (mqtt_bridge.py's _maybe_record does
    this via hass.async_create_task).
    """
    try:
        media_root = _recordings_root(hass, path_root)
        await hass.async_add_executor_job(lambda: media_root.mkdir(parents=True, exist_ok=True))

        ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
        filename  = f"{_safe_slug(area)}_{_safe_slug(name)}_{ts}.mp4"
        full_path = media_root / filename

        try:
            camera_entity = get_camera_from_entity_id(hass, camera_entity_id)
            await camera_entity.async_create_stream()
            await asyncio.sleep(STREAM_WARMUP_S)
        except Exception as warmup_err:  # noqa: BLE001 — warm-up is best-effort only
            _LOGGER.warning(
                "Stream warm-up failed for %s/%s (%s) — recording anyway, may hit the "
                "non-monotonic-dts muxer failure on a cold start",
                area, name, warmup_err,
            )

        await hass.services.async_call(
            "camera", "record",
            {"entity_id": camera_entity_id, "filename": str(full_path), "duration": duration_s},
            blocking=False,
        )
        _LOGGER.info("Recording started: %s/%s -> %s (%ds)", area, name, full_path, duration_s)
        return str(full_path)
    except Exception as err:  # noqa: BLE001 — a failed recording must never break the alert pipeline
        _LOGGER.error("Recording failed to start for %s/%s: %s", area, name, err)
        return None


def _do_cleanup(hass: HomeAssistant, path_root: str, retention_days: int) -> dict:
    """Blocking part of housekeeping — runs in the executor."""
    media_root = _recordings_root(hass, path_root)
    result = {
        "deleted_by_retention": 0, "deleted_by_space": 0,
        "low": False, "free_gb": None, "free_pct": None,
    }
    if not media_root.exists():
        return result

    cutoff = time.time() - retention_days * 86400
    files = sorted(media_root.glob("*.mp4"), key=lambda p: p.stat().st_mtime)

    remaining: list[Path] = []
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                result["deleted_by_retention"] += 1
            else:
                remaining.append(f)
        except OSError:
            pass  # best-effort — one bad file must never block the rest

    try:
        usage = shutil.disk_usage(media_root)
        free_gb  = usage.free / (1024 ** 3)
        free_pct = usage.free / usage.total * 100
        result["free_gb"], result["free_pct"] = free_gb, free_pct
        result["low"] = free_gb < LOW_DISK_GB or free_pct < LOW_DISK_PERCENT
    except OSError:
        return result

    if result["low"]:
        # Aggressively prune oldest-first, regardless of retention, until back
        # above threshold or nothing left to delete — don't just alert and let
        # the disk actually fill toward the root filesystem.
        for f in remaining:
            try:
                usage = shutil.disk_usage(media_root)
                if usage.free / (1024 ** 3) >= LOW_DISK_GB and usage.free / usage.total * 100 >= LOW_DISK_PERCENT:
                    break
                f.unlink()
                result["deleted_by_space"] += 1
            except OSError:
                pass

    return result


async def housekeep_recordings(
    hass: HomeAssistant,
    path_root: str,
    retention_days: int,
    send_tech_alert: Callable[[str, str], Awaitable[None]],
) -> None:
    """Delete recordings past retention_days, then check free disk space on that
    volume. If still low after retention-based cleanup, aggressively prune the
    oldest remaining files and send a tech alert via send_tech_alert(subject, detail)
    — reuse whatever tech-alert channel the caller already has
    (e.g. TuyaMQTTBridge._send_tech_alert).
    """
    try:
        result = await hass.async_add_executor_job(_do_cleanup, hass, path_root, retention_days)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Recording housekeeping failed: %s", err)
        return

    if result["deleted_by_retention"] or result["deleted_by_space"]:
        _LOGGER.info(
            "Recording housekeeping: %d deleted (retention), %d deleted (low disk space)",
            result["deleted_by_retention"], result["deleted_by_space"],
        )

    if result["low"]:
        await send_tech_alert(
            "Recording storage low on disk space",
            f"Free space is low ({result['free_gb']:.1f} GB, {result['free_pct']:.1f}%) even "
            f"after retention-based cleanup. Deleted {result['deleted_by_space']} additional "
            f"oldest recording(s) to free space. Consider shortening the retention period or "
            f"recording duration, or freeing disk space on this volume.",
        )
