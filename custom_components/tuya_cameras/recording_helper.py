"""Local motion recording (on confirmed human detection) and its housekeeping
(retention-based cleanup, low-disk-space alerting + pruning).

Recorder (2026-11-12): recordings are made by the ffmpeg command-line tool that ships with
Home Assistant (the same binary HA's ffmpeg integration and our burst capture use), not by
HA's camera.record service. camera.record remuxes through PyAV and aborts the whole recording
at the first frame whose timestamp goes backwards ("non monotonically increasing dts to
muxer"), which Tuya's cloud relay produces now and then (2 of 11 recordings failed on
2026-11-12, both ~15 s in, nothing salvageable). The ffmpeg CLI instead repairs such
timestamps and keeps writing ("Non-monotonic DTS … changing to …"). It also needs no
warm-up: it opens its own connection on a freshly signed URL, so the recording starts
~2 s after start_recording() is called instead of 12 s, and there is no shared HA Stream
whose recorder can get stuck. camera.record stays as a fallback when ffmpeg fails
(see _record_with_ha_stream); the notes below on warm-up and stale URLs apply to it.

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
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Awaitable, Callable

from homeassistant.components.camera import get_camera_from_entity_id
from homeassistant.components.stream.const import RECORDER_PROVIDER
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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

# A recording that dies mid-way (e.g. the Tuya relay's "non monotonically increasing
# dts" muxer error) leaves its half-written <name>.mp4.tmp behind, and retention
# cleanup only ever looked at *.mp4. A live recording finishes within duration +
# RECORD_GRACE_S, so any .mp4.tmp older than this is an orphan and safe to delete.
ORPHAN_TMP_AGE_S = 3600

# ffmpeg recorder (see module docstring "Recorder"). Connecting to the Tuya relay takes
# ~2-3 s; the allowance covers that plus finalizing the file. Past duration + allowance,
# ffmpeg gets SIGINT (it then finalizes a playable file of what it has) and, if it still
# hangs, is killed. Anything smaller than FFMPEG_MIN_BYTES is a header without video.
FFMPEG_ALLOWANCE_S    = 30
FFMPEG_STOP_GRACE_S   = 10
FFMPEG_IO_TIMEOUT_US  = 10_000_000   # RTSP socket timeout: a silent relay fails fast
FFMPEG_MIN_BYTES      = 10_000

_STREAM_URL_RE = re.compile(r"rtsps?://\S+")   # signed URLs carry tokens — never log them


def _recordings_root(hass: HomeAssistant, path_root: str) -> Path:
    """Root directory for a given recording_path config value, under /config/media."""
    return Path(hass.config.path("media", path_root))


def _safe_slug(text: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in text).strip("_")
    return slug or "camera"


def file_label(text: str) -> str:
    """'Garden ' + ' Front Camera' -> 'Garden_Front_Camera': a file-name-safe label with
    runs of underscores collapsed (area names can carry stray spaces)."""
    return "_".join(part for part in _safe_slug(text).split("_") if part) or "camera"


def file_stamp() -> str:
    """Timestamp that STARTS every saved image/recording name, in Home Assistant's local
    time zone (the time people see in their camera app), e.g. 2026-11-11_17-54-52.

    Leading with the time makes alphabetical order equal time order — HA's media browser
    can only sort by name — and lets gallery cards parse the date from the name.
    """
    return dt_util.now().strftime("%Y-%m-%d_%H-%M-%S")


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


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


async def _record_with_ffmpeg(
    hass: HomeAssistant, camera_entity_id: str, full_path: Path, duration_s: int,
) -> tuple[bool, str]:
    """Record duration_s seconds straight from the camera's stream with the ffmpeg CLI
    (see module docstring "Recorder"). Writes <name>.mp4.tmp and renames it to full_path
    only once ffmpeg has finalized it. Returns (True, note) when saved — note says how
    many timestamps ffmpeg had to repair, if any — else (False, reason)."""
    from homeassistant.components.ffmpeg import get_ffmpeg_manager

    url = await get_camera_from_entity_id(hass, camera_entity_id).stream_source()
    if not url:
        return False, "camera has no stream URL"
    binary = get_ffmpeg_manager(hass).binary
    tmp = full_path.with_name(full_path.name + ".tmp")

    args = [binary, "-hide_banner", "-loglevel", "warning"]
    if url.startswith("rtsp"):
        args += ["-rtsp_transport", "tcp", "-timeout", str(FFMPEG_IO_TIMEOUT_US)]
    args += [
        "-i", url,
        "-t", str(duration_s),
        "-map", "0:v:0", "-c", "copy",        # video only, as camera.record saved it
        "-movflags", "+faststart",            # index up front: plays while still loading
        "-f", "mp4", "-y", str(tmp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.ensure_future(proc.stderr.read())  # drain, or a chatty ffmpeg blocks
    killed = False
    try:
        await asyncio.wait_for(proc.wait(), duration_s + FFMPEG_ALLOWANCE_S)
    except asyncio.TimeoutError:
        proc.send_signal(signal.SIGINT)  # ffmpeg finalizes what it has on SIGINT
        try:
            await asyncio.wait_for(proc.wait(), FFMPEG_STOP_GRACE_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            killed = True
    stderr = _STREAM_URL_RE.sub("<stream>", (await stderr_task).decode(errors="replace"))

    size = await hass.async_add_executor_job(_file_size, tmp)
    if killed or size < FFMPEG_MIN_BYTES:
        await hass.async_add_executor_job(_remove, tmp)
        last = stderr.strip().splitlines()[-1][:300] if stderr.strip() else "no output"
        if killed:
            return False, f"ffmpeg hung and was killed after {duration_s + FFMPEG_ALLOWANCE_S}s"
        return False, f"ffmpeg exit {proc.returncode}, {size} bytes written: {last}"

    await hass.async_add_executor_job(tmp.replace, full_path)
    fixes = stderr.lower().count("non-monotonic dts")
    note = f"ffmpeg exit {proc.returncode}"
    if fixes:
        note += f", repaired {fixes} out-of-order timestamp(s)"
    return True, note


async def start_recording(
    hass: HomeAssistant,
    camera_entity_id: str,
    area: str,
    name: str,
    duration_s: int,
    path_root: str,
) -> tuple[str | None, str | None]:
    """Record duration_s seconds of the camera and verify the result: ffmpeg first, HA's
    camera.record as the fallback (see module docstring "Recorder").

    Returns (saved_path, None) once the recording has finished AND a non-empty .mp4
    exists on disk, (None, None) if skipped because this camera is already being recorded
    by an earlier event, otherwise (None, reason). Never raises.

    Takes about duration_s + a few seconds (ffmpeg), or up to twice that plus
    STREAM_WARMUP_S when the fallback runs, so callers must NOT await it inline on the
    alert-email path; fire it as a background task instead (mqtt_bridge.py's
    _maybe_record does this via hass.async_create_task).
    """
    if camera_entity_id in _ACTIVE_RECORDINGS:
        _LOGGER.info("Recording skipped for %s/%s: already recording this camera", area, name)
        return None, None
    _ACTIVE_RECORDINGS.add(camera_entity_id)
    try:
        media_root = _recordings_root(hass, path_root)
        await hass.async_add_executor_job(lambda: media_root.mkdir(parents=True, exist_ok=True))

        filename  = f"{file_stamp()}_{file_label(f'{area} {name}')}.mp4"
        full_path = media_root / filename

        t0 = time.monotonic()
        _LOGGER.info("Recording started: %s/%s -> %s (%ds, ffmpeg)", area, name, full_path, duration_s)
        try:
            saved, note = await _record_with_ffmpeg(hass, camera_entity_id, full_path, duration_s)
        except Exception as err:  # noqa: BLE001 — fall back to camera.record below
            saved, note = False, _STREAM_URL_RE.sub("<stream>", str(err))
        if saved:
            size = await hass.async_add_executor_job(_file_size, full_path)
            _LOGGER.info(
                "Recording saved: %s/%s -> %s (%d KB, %.0fs, %s)",
                area, name, full_path, size // 1024, time.monotonic() - t0, note,
            )
            return str(full_path), None

        _LOGGER.warning(
            "ffmpeg recording failed for %s/%s (%s) — falling back to camera.record", area, name, note,
        )
        saved, reason = await _record_with_ha_stream(
            hass, camera_entity_id, area, name, media_root, full_path, duration_s,
        )
        if saved:
            return str(full_path), None
        reason = f"ffmpeg: {note}; camera.record fallback: {reason}"
        _LOGGER.error("Recording failed for %s/%s: %s", area, name, reason)
        return None, reason
    except Exception as err:  # noqa: BLE001 — a failed recording must never break the alert pipeline
        _LOGGER.error("Recording failed for %s/%s: %s", area, name, err)
        return None, str(err)
    finally:
        _ACTIVE_RECORDINGS.discard(camera_entity_id)


async def _record_with_ha_stream(
    hass: HomeAssistant,
    camera_entity_id: str,
    area: str,
    name: str,
    media_root: Path,
    full_path: Path,
    duration_s: int,
) -> tuple[bool, str | None]:
    """Fallback recorder: HA's camera.record service. Returns (True, None) once the
    recording has finished AND a non-empty .mp4 exists, else (False, reason). Never raises.

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
    module docstring "Stale RTSP URL refresh" and "Stream warm-up").
    """
    try:
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
        _LOGGER.info("Recording started: %s/%s -> %s (%ds, camera.record)", area, name, full_path, duration_s)

        done, _ = await asyncio.wait({record_task}, timeout=duration_s + RECORD_GRACE_S)
        if not done:
            # Not cancelled (that would leave HA's recorder attached); removing the recorder
            # instead lets HA's own camera.record call finish cleanly.
            await _free_own_recorder(hass, camera_entity_id, str(full_path))
            return False, f"still not finished {duration_s + RECORD_GRACE_S}s after it started (stream stalled?)"
        record_task.result()  # re-raises the service's own error, e.g. path not allowed

        size = await hass.async_add_executor_job(_file_size, full_path)
        if not size:
            return False, "camera.record finished but no video file was written (stream dropped mid-recording?)"
        _LOGGER.info("Recording saved: %s/%s -> %s (%d KB, camera.record)", area, name, full_path, size // 1024)
        return True, None
    except Exception as err:  # noqa: BLE001 — a failed recording must never break the alert pipeline
        return False, _STREAM_URL_RE.sub("<stream>", str(err))


def _do_cleanup(hass: HomeAssistant, path_root: str, retention_days: int) -> dict:
    """Blocking part of housekeeping — runs in the executor."""
    media_root = _recordings_root(hass, path_root)
    result = {
        "deleted_by_retention": 0, "deleted_by_space": 0, "deleted_orphan_tmp": 0,
        "low": False, "free_gb": None, "free_pct": None,
    }
    if not media_root.exists():
        return result

    tmp_cutoff = time.time() - ORPHAN_TMP_AGE_S
    for f in media_root.glob("*.mp4.tmp"):
        try:
            if f.stat().st_mtime < tmp_cutoff:
                f.unlink()
                result["deleted_orphan_tmp"] += 1
        except OSError:
            pass  # best-effort — one bad file must never block the rest

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

    if result["deleted_by_retention"] or result["deleted_by_space"] or result["deleted_orphan_tmp"]:
        _LOGGER.info(
            "Recording housekeeping: %d deleted (retention), %d deleted (low disk space), "
            "%d orphaned .mp4.tmp removed (failed recordings)",
            result["deleted_by_retention"], result["deleted_by_space"], result["deleted_orphan_tmp"],
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
