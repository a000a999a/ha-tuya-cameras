"""Local motion recording (camera.record on confirmed human detection) and its
housekeeping (retention-based cleanup, low-disk-space alerting + pruning).

Added 2026-08-31. Applies to both the MQTT and ONVIF motion paths — callers invoke
start_recording() only after a human has already been confirmed by AI, deliberately
not on raw/unfiltered motion, so a false positive (wind, shadows, an animal) never
burns a full recording. Video is referenced by its saved path in the alert email,
never attached (large files, most mail clients don't render video inline).

Storage root: recordings are stored under hass.config.path("media", path_root)
(/config/media/<path_root> inside the container). HA's camera.record service
enforces a security allowlist (stream.async_record() rejects paths outside it):

    hac.allowlist_external_dirs = {hass.config.path("www"), *hac.media_dirs.values()}

media_dirs["local"] resolves to /config/media on this Docker install (the
entire /config is bind-mounted to the host), so /config/media is already on
the allowlist with no configuration.yaml changes needed. Using /config/media
instead of /config/www means recordings appear in HA's native Media Browser
(Media sidebar → Local media) with thumbnail previews and in-browser playback.
Confirmed live 2026-11-03: /config/media is host-persistent and allowlisted.

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
(confirmed working: the one fully successful recording so far, an ONVIF
camera, went through an already-live connection, not a cold
one). So start_recording() now pre-warms the same cached Stream itself —
calling camera.async_create_stream() and giving it a few seconds — before
calling the camera.record service, so the record call reuses an
already-stabilizing connection instead of cold-starting its own.

Stale RTSP URL refresh: confirmed live 2026-09-02/03 that camera.async_create_stream() alone
isn't enough — it just returns whatever RTSP URL is already cached for that camera (via the
official Tuya integration / go2rtc), and if that URL's signed token has already expired, the
stream fails outright with `stream_worker ... 404 Not Found`, over and over, no matter how long
the warm-up sleep is. Confirmed on three different cameras across one evening (Camera Door,
Panorama Camera escadas, Lago Camera), and confirmed NOT self-recovering even after 9+ minutes of
automatic retries. Worse, once a recording's stream never successfully opens, its recorder object
never reaches finish_writing() (see stream/recorder.py), so HA never releases the "recording"
flag — every later recording attempt on that SAME camera then fails too, with
`HomeAssistantError: Stream already recording to ...`, until something forces a fresh Stream
(a full HA restart — confirmed working but a heavy hammer, and not durable since the underlying
cause recurs).

The first fix (Sept 2026) called `homeassistant.update_entity` before recording. That does not
work, confirmed live 2026-11-10 (Door, Cage, Bèrgerie: first recording fine, every later one 404 →
"Stream already recording"): Camera.async_create_stream() only reads stream_source() when it
CREATES the Stream, then caches that Stream (and the signed URL inside it, Stream.source) for the
entity's lifetime. Refreshing the entity changes nothing in the cached Stream. _prepare_stream()
now fetches a fresh URL itself and writes it into the cached Stream (update_source() if the worker
is running, e.g. a live view; plain assignment if it is stopped — update_source() on a stopped
worker would leave a fast-restart flag set that makes the next stop() restart instead). It also
frees a wedged recorder left by an earlier failed attempt, and start_recording() frees its own
recorder when it times out, so a dead stream no longer retries the expired URL forever and no
longer blocks the next recording.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from homeassistant.components.camera import get_camera_from_entity_id
from homeassistant.components.stream.const import RECORDER_PROVIDER
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Cameras with a start_recording() in progress. A second human event on the same camera while
# one is running is skipped quietly instead of colliding with HA's one-recorder-per-stream rule.
_ACTIVE_RECORDINGS: set[str] = set()

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

# How long past the requested duration to wait for camera.record to finish before
# declaring the recording failed: HA's 5 s lookback, waiting for the next HLS segment,
# and the final remux/rename of the .mp4.tmp all happen after the recording window.
RECORD_GRACE_S = 60


def _recordings_root(hass: HomeAssistant, path_root: str) -> Path:
    """Root directory for a given recording_path config value, under /config/media."""
    return Path(hass.config.path("media", path_root))


def _safe_slug(text: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in text).strip("_")
    return slug or "camera"


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def _prepare_stream(
    hass: HomeAssistant, camera_entity_id: str, area: str, name: str, media_root: Path,
) -> None:
    """Point the camera's cached Stream at a freshly signed URL and free a wedged recorder
    of ours (see module docstring "Stale RTSP URL refresh"). Raises if the camera is busy
    with a recording that is not ours."""
    camera = get_camera_from_entity_id(hass, camera_entity_id)
    stream = camera.stream
    if stream is None:
        return  # camera.record creates the Stream from a fresh stream_source() itself

    recorder = stream.outputs().get(RECORDER_PROVIDER)
    if recorder is not None:
        video_path = str(getattr(recorder, "video_path", ""))
        if not video_path.startswith(str(media_root)):
            raise RuntimeError(f"camera is already recording to {video_path} (not started by tuya_cameras)")
        # Ours, but no start_recording() is running for this camera any more → left over
        # from a failed attempt (e.g. before an HA restart); it would block this one.
        _LOGGER.warning("Freeing stuck recorder on %s/%s (was writing %s)", area, name, video_path)
        await stream.remove_provider(recorder)

    source = await camera.stream_source()
    if not source:
        return
    if stream.outputs():
        stream.update_source(source)  # worker running (live view): restart on the new URL
    else:
        stream.source = source        # worker stopped: its next start() uses the new URL


async def _free_own_recorder(hass: HomeAssistant, camera_entity_id: str, video_path: str) -> None:
    """Drop the recorder we added if it is still attached. Ends HA's blocked camera.record
    call and stops the stream worker from retrying a dead URL indefinitely."""
    try:
        stream = get_camera_from_entity_id(hass, camera_entity_id).stream
        recorder = stream.outputs().get(RECORDER_PROVIDER) if stream else None
        if recorder is not None and getattr(recorder, "video_path", None) == video_path:
            await stream.remove_provider(recorder)
    except Exception as err:  # noqa: BLE001 — best-effort cleanup
        _LOGGER.warning("Could not free recorder on %s: %s", camera_entity_id, err)


async def start_recording(
    hass: HomeAssistant,
    camera_entity_id: str,
    area: str,
    name: str,
    duration_s: int,
    path_root: str,
) -> tuple[str | None, str | None]:
    """Record duration_s seconds via camera.record and verify the result.

    Returns (saved_path, None) once the recording has finished AND a non-empty .mp4
    exists on disk, (None, None) if skipped because this camera is already being recorded
    by an earlier event, otherwise (None, reason). Never raises.

    Failure detection (2026-11-10): camera.record used to be called with blocking=False,
    i.e. fire-and-forget. HA then runs the service in its own task, and any error — even
    the immediate "Can't write …, no access to path!" — is caught by HA core and only
    written to home-assistant.log as "Error executing service". Our code had already
    logged "Recording started" and returned the path, so the caller's failure alert never
    fired: every recording failed silently from 2026-11-03 to 2026-11-10. Now camera.record
    runs blocking (HA's handler only returns once the whole recording is written, so errors
    surface here), and the file itself is checked afterwards, which also catches a stream
    that dies mid-recording and leaves only an orphaned .mp4.tmp.

    Refreshes the camera's RTSP URL and pre-warms its stream before recording (see
    module docstring "Stale RTSP URL refresh" and "Stream warm-up"). With the warm-up
    plus the recording itself this takes about STREAM_WARMUP_S + duration_s, so callers
    must NOT await it inline on the alert-email path; fire it as a background task
    instead (mqtt_bridge.py's _maybe_record does this via hass.async_create_task).
    """
    if camera_entity_id in _ACTIVE_RECORDINGS:
        _LOGGER.info("Recording skipped for %s/%s: already recording this camera", area, name)
        return None, None
    _ACTIVE_RECORDINGS.add(camera_entity_id)
    try:
        media_root = _recordings_root(hass, path_root)
        await hass.async_add_executor_job(lambda: media_root.mkdir(parents=True, exist_ok=True))

        ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
        filename  = f"{_safe_slug(area)}_{_safe_slug(name)}_{ts}.mp4"
        full_path = media_root / filename

        await _prepare_stream(hass, camera_entity_id, area, name, media_root)

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

        record_task = hass.async_create_task(
            hass.services.async_call(
                "camera", "record",
                {"entity_id": camera_entity_id, "filename": str(full_path), "duration": duration_s},
                blocking=True,
            ),
            f"tuya_cameras record {camera_entity_id}",
        )
        # If we give up waiting below, the task keeps running; consume its eventual
        # exception so asyncio doesn't log "Task exception was never retrieved".
        record_task.add_done_callback(lambda t: t.cancelled() or t.exception())
        _LOGGER.info("Recording started: %s/%s -> %s (%ds)", area, name, full_path, duration_s)

        done, _ = await asyncio.wait({record_task}, timeout=duration_s + RECORD_GRACE_S)
        if not done:
            # Not cancelled (that would leave HA's recorder attached); removing the recorder
            # instead lets HA's own camera.record call finish cleanly.
            await _free_own_recorder(hass, camera_entity_id, str(full_path))
            reason = f"still not finished {duration_s + RECORD_GRACE_S}s after it started (stream stalled?)"
            _LOGGER.error("Recording failed for %s/%s: %s", area, name, reason)
            return None, reason
        record_task.result()  # re-raises the service's own error, e.g. path not allowed

        size = await hass.async_add_executor_job(_file_size, full_path)
        if not size:
            reason = "camera.record finished but no video file was written (stream dropped mid-recording?)"
            _LOGGER.error("Recording failed for %s/%s: %s — %s", area, name, reason, full_path)
            return None, reason
        _LOGGER.info("Recording saved: %s/%s -> %s (%d KB)", area, name, full_path, size // 1024)
        return str(full_path), None
    except Exception as err:  # noqa: BLE001 — a failed recording must never break the alert pipeline
        _LOGGER.error("Recording failed for %s/%s: %s", area, name, err)
        return None, str(err)
    finally:
        _ACTIVE_RECORDINGS.discard(camera_entity_id)


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


def housekeep_snapshots(hass: HomeAssistant, retention_days: int = 7) -> int:
    """Delete debug snapshot files older than retention_days. Returns count deleted.
    Blocking — run in the executor."""
    snap_dir = Path(hass.config.path("debug_snapshots"))
    if not snap_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for f in snap_dir.glob("snap_*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted
