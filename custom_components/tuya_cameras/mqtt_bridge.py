"""
Real-time Tuya MQTT bridge — listens for motion events, optionally filters via
AI human detection, then sends email alerts.
Reconnects automatically 10 minutes before MQTT credentials expire (~2h TTL).
AES-128-ECB key = mqtt_password[8:24] (session-specific).

AI mode (ai_client is not None):
  image present → infer → human: email annotated image / other: discard
  image absent  → discard silently
  service error → fail-open: email with original image

Non-AI mode: all motion events trigger email as before.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

from .ai_client import AIClient
from .ai_stats import AIStats
from .camera_api import CameraAPI
from .const import EVENT_AI_UPDATED
from .notify import Notifier

_LOGGER = logging.getLogger(__name__)

RECONNECT_BUFFER_S        = 600    # refresh creds 10 min before expiry
RETRY_DELAY_S             = 30
WATCHDOG_SILENCE_S        = 3600   # alert if client is DISCONNECTED + silent for 1 h
WATCHDOG_SILENCE_CONN_S   = 28800  # alert if client is CONNECTED but silent for 8 h (cameras quiet)
WATCHDOG_CHECK_S          = 300    # check every 5 min
TECH_ALERT_COOLDOWN       = 1800   # suppress duplicate tech alerts for 30 min


class TuyaMQTTBridge:
    """Manages the Tuya MQTT connection as an async HA background task."""

    def __init__(
        self,
        hass: Any,
        tuya_client: Any,
        camera_api: CameraAPI,
        notifier: Notifier,
        recipients_cfg: dict,
        uid: str,
        access_id: str,
        core_coord: Any = None,
        cam_coord: Any = None,
        ai_client: AIClient | None = None,
        ai_stats: AIStats | None = None,
        alerts_enabled: bool = True,
        entry_label: str = "",
        animal_cfg: dict | None = None,
    ) -> None:
        self._hass           = hass
        self._tuya_client    = tuya_client
        self._camera_api     = camera_api
        self._notifier       = notifier
        self._recipients_cfg = recipients_cfg  # {area: {human: "...", tech: "..."}}
        self._uid            = uid
        self._access_id      = access_id
        self._core_coord     = core_coord      # coordinator for this project's device list only
        self._cam_coord      = cam_coord       # cameras coordinator (has entity registry fallback)
        self._ai_client      = ai_client   # None = AI disabled
        self._ai_stats       = ai_stats
        self._alerts_enabled = alerts_enabled
        self._entry_label    = entry_label or uid  # used in tech alerts + motion email source tag
        self._animal_cfg     = animal_cfg or {}    # {device_id: {enabled, classes}}
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._local_keys: dict[str, str] = {}    # device_id → local_key
        self._product_ids: dict[str, str] = {}  # device_id → product_id (for v4 blob decrypt)
        self._uuids: dict[str, str] = {}         # device_id → uuid
        self._last_msg_at: float = time.monotonic()
        self._last_tech_alert_at: float = 0.0
        self._current_client: Any = None  # live paho client; None when disconnected
        self._last_healed_at: float = 0.0  # last time watchdog forced a reconnect to heal silence

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, hass: Any) -> None:
        self._task          = hass.async_create_background_task(self._run(),      "tuya_cameras_mqtt_bridge")
        self._watchdog_task = hass.async_create_background_task(self._watchdog(), "tuya_cameras_mqtt_watchdog")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        _LOGGER.info("Tuya MQTT bridge starting")
        await self._hass.async_add_executor_job(self._fetch_local_keys)
        reconnect_delay = 5  # seconds between reconnects after a drop
        while True:
            try:
                creds = await self._hass.async_add_executor_job(self._fetch_creds)
                if not creds:
                    _LOGGER.error("MQTT credentials unavailable — retrying in %ds", RETRY_DELAY_S)
                    await self._send_tech_alert(
                        "MQTT credentials unavailable",
                        "Could not fetch MQTT credentials from Tuya API. Bridge will retry automatically.",
                    )
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue

                expire_s    = creds.get("expire_time", 7200)
                refresh_at  = time.monotonic() + expire_s - RECONNECT_BUFFER_S
                full_pw     = creds["password"]
                key         = full_pw[8:24].encode()   # AES-128-ECB envelope key
                _LOGGER.debug("MQTT password len=%d", len(full_pw))

                loop = asyncio.get_event_loop()
                disconnect_event = asyncio.Event()

                client = await self._hass.async_add_executor_job(self._connect, creds)
                if client is None:
                    await self._send_tech_alert(
                        "MQTT connection failed",
                        "Could not connect to Tuya MQTT broker. Bridge will retry automatically.",
                    )
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue

                def on_message(c, userdata, msg, _pw=full_pw):
                    asyncio.run_coroutine_threadsafe(
                        self._handle(msg.payload, key, _pw), loop
                    )

                def on_disconnect(c, userdata, *args):
                    _LOGGER.warning("MQTT disconnected — will reconnect in %ds", reconnect_delay)
                    loop.call_soon_threadsafe(disconnect_event.set)

                client.on_message    = on_message
                client.on_disconnect = on_disconnect
                self._current_client = client  # watchdog reads this to distinguish quiet vs broken

                _LOGGER.info("MQTT bridge connected. Creds expire in %ds", expire_s)

                # Wait for disconnect signal or credential expiry
                cred_timeout = refresh_at - time.monotonic()
                try:
                    await asyncio.wait_for(disconnect_event.wait(), timeout=cred_timeout)
                    # Disconnected — wait briefly then reconnect with same creds
                    self._current_client = None
                    await self._hass.async_add_executor_job(self._disconnect, client)
                    await asyncio.sleep(reconnect_delay)
                    continue
                except asyncio.TimeoutError:
                    pass  # Credentials about to expire — fall through to refresh

                self._current_client = None
                await self._hass.async_add_executor_job(self._disconnect, client)
                _LOGGER.info("Refreshing MQTT credentials")

            except asyncio.CancelledError:
                _LOGGER.info("MQTT bridge stopped")
                return
            except Exception as err:
                _LOGGER.error("MQTT bridge error: %s — retrying in %ds", err, RETRY_DELAY_S)
                await self._send_tech_alert("MQTT bridge error", f"Bridge encountered an error and will retry: {err}")
                await asyncio.sleep(RETRY_DELAY_S)

    # ── Connection helpers (blocking — called via executor) ───────────────────

    def _fetch_local_keys(self) -> None:
        """Fetch and cache local_key for every camera — used to decrypt v4 file blobs.

        Calls /v1.0/devices/{id} per camera (proven to return local_key).
        Only runs once at bridge start — 1 API call per camera, acceptable.
        """
        try:
            devices: list[dict] = []
            if self._core_coord and self._core_coord.data:
                devices = self._core_coord.data.get("devices", [])
            else:
                # fallback: first available core entry (single-project installs)
                for entry_data in self._hass.data.get("tuya_home_core", {}).values():
                    coord = entry_data.get("coordinator")
                    if coord and coord.data:
                        devices = coord.data.get("devices", [])
                        break

            for dev in devices:
                dev_id = dev.get("id") or dev.get("devId", "")
                if not dev_id:
                    continue
                # coordinator data rarely includes local_key — try direct fetch
                lk  = dev.get("local_key", "")
                pid = dev.get("product_id", "")
                uid = dev.get("uuid", "")
                if not lk:
                    try:
                        r   = self._tuya_client.cloudrequest(f"/v1.0/devices/{dev_id}")
                        res = (r or {}).get("result", {})
                        lk  = res.get("local_key", "")
                        pid = pid or res.get("product_id", "")
                        uid = uid or res.get("uuid", "")
                    except Exception:
                        pass
                if lk:
                    self._local_keys[dev_id] = lk
                if pid:
                    self._product_ids[dev_id] = pid
                if uid:
                    self._uuids[dev_id] = uid

            _LOGGER.debug(
                "Local keys cached for %d/%d devices (%d product_ids, %d uuids)",
                len(self._local_keys), len(devices), len(self._product_ids), len(self._uuids)
            )
        except Exception as err:
            _LOGGER.debug("Local key fetch failed: %s", err)

    def _fetch_creds(self) -> dict | None:
        try:
            r = self._tuya_client.cloudrequest(
                "/v1.0/open-hub/access/config",
                action="POST",
                post={
                    "uid":       self._uid,
                    "link_id":   self._access_id,
                    "link_type": "mqtt",
                    "topics":    "device",
                },
            )
            if r and r.get("success"):
                result = r["result"]
                _LOGGER.debug("MQTT creds fields: %s", list(result.keys()))
                return result
            _LOGGER.error("MQTT creds API error: %s", r)
        except Exception as err:
            _LOGGER.error("MQTT creds fetch exception: %s", err)
        return None

    def _connect(self, creds: dict):
        try:
            import paho.mqtt.client as mqtt

            host  = creds["url"].replace("ssl://", "").split(":")[0]
            port  = int(creds["url"].split(":")[-1])
            topic = creds["source_topic"]["device"]

            rc_box = [None]

            def on_connect(c, userdata, flags, reason_code, properties=None):
                failed = getattr(reason_code, "is_failure", bool(reason_code))
                rc_box[0] = 1 if failed else 0
                if not failed:
                    c.subscribe(topic, qos=1)
                    _LOGGER.info("MQTT subscribed to %s", topic)
                else:
                    _LOGGER.error("MQTT connect failed: %s", reason_code)

            try:
                client = mqtt.Client(
                    client_id=creds["client_id"],
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    reconnect_on_failure=False,
                )
            except (AttributeError, TypeError):
                client = mqtt.Client(client_id=creds["client_id"])

            client.username_pw_set(creds["username"], creds["password"])
            client.tls_set()
            client.on_connect = on_connect

            # m1.tuyaeu.com resolves IPv6-first; on a dual-stack host paho
            # connects via IPv6, TLS succeeds, but Tuya's broker delivers no
            # events on IPv6 sessions — silent MQTT with healthy keepalive.
            # Patch getaddrinfo for the duration of connect() so paho picks
            # an IPv4 address, while still passing the hostname to TLS for
            # correct SNI / certificate verification.
            _orig_gai = socket.getaddrinfo
            def _ipv4_gai(h, p, family=0, *a, **kw):
                if h == host:
                    return _orig_gai(h, p, socket.AF_INET, *a, **kw)
                return _orig_gai(h, p, family, *a, **kw)
            socket.getaddrinfo = _ipv4_gai
            try:
                client.connect(host, port, keepalive=60)
            finally:
                socket.getaddrinfo = _orig_gai
            client.loop_start()

            deadline = time.time() + 15
            while rc_box[0] is None and time.time() < deadline:
                time.sleep(0.2)

            if rc_box[0] != 0:
                client.loop_stop()
                return None
            return client
        except Exception as err:
            _LOGGER.error("MQTT connect exception: %s", err)
            return None

    @staticmethod
    def _disconnect(client) -> None:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    # ── Message handling (async, on HA event loop) ────────────────────────────

    async def _handle(self, payload: bytes, key: bytes, full_password: str = "") -> None:
        self._last_msg_at = time.monotonic()  # watchdog heartbeat
        try:
            envelope = json.loads(payload)
        except Exception:
            return

        raw_data = envelope.get("data", {})
        msg      = self._decrypt(raw_data, key)

        dev_id = msg.get("devId", "")
        status = msg.get("status", [])
        if not dev_id or not isinstance(status, list):
            return

        # Build camera list before the code filter so we can log unknown codes from known cameras.
        # Prefer cameras coordinator — has entity registry fallback when Tuya device API is down.
        cameras: dict = {}
        if self._cam_coord and self._cam_coord.data:
            cameras = self._cam_coord.data.get("cameras", {})
        elif self._core_coord and self._core_coord.data:
            cam_list = self._camera_api.cameras_from_devices(
                self._core_coord.data.get("devices", []),
                self._core_coord.data.get("areas", {}),
            )
            cameras = {c["id"]: c for c in cam_list}

        motion_dps = [s for s in status if s.get("code") in ("initiative_message", "movement_detect_pic")]
        if not motion_dps:
            if dev_id in cameras:
                codes = [s.get("code") for s in status if s.get("code")]
                _LOGGER.debug("Known camera %s sent DPS codes %r — no motion codes, skipping", dev_id, codes)
            return

        cam = cameras.get(dev_id)
        if not cam:
            _LOGGER.debug("Motion from unknown device %s — skipping", dev_id)
            return

        area = cam.get("area", "Unknown")
        name = cam["name"]

        newest    = max(motion_dps, key=lambda s: s.get("t", 0))
        t_ms      = newest.get("t", int(time.time())) * 1000
        raw_v     = newest.get("value", "")
        ev        = self._parse_motion_value(
            newest["code"], raw_v, key,
            access_id=getattr(self._tuya_client, "apiKey", ""),
            access_secret=getattr(self._tuya_client, "apiSecret", ""),
            device_key=self._local_keys.get(dev_id, ""),
            event_t=newest.get("t", 0),
            full_password=full_password,
            product_id=self._product_ids.get(dev_id, ""),
            device_uuid=self._uuids.get(dev_id, ""),
        )
        age_s  = (time.time() * 1000 - t_ms) / 1000
        ev_ts  = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        _LOGGER.debug(
            "Motion %s/%s: code=%s bucket=%r files=%r",
            area, name, newest["code"], ev.get("bucket"), ev.get("files"),
        )

        img_bytes     = None
        snap_note     = ""
        prefetched_ai: dict | None = None

        if ev.get("bucket") and ev.get("files"):
            if ev["bucket"] == "__inline_jpeg__":
                # v4.0 inline thumbnail embedded in MQTT message
                raw_inline = ev["files"][0]
                if isinstance(raw_inline, (bytes, str)):
                    img_bytes = raw_inline if isinstance(raw_inline, bytes) else raw_inline.encode("latin-1")
                    _LOGGER.debug("Motion %s/%s: inline JPEG from v4 blob (%d bytes)", area, name, len(img_bytes))
            else:
                parts     = ev["files"][0] if ev.get("files") else []
                file_path = parts[0] if len(parts) > 0 else ""
                file_key  = parts[1] if len(parts) > 1 else ""
                if "?param=" in file_path and not file_key:
                    # Signed CDN URL — confirmed blocked (403) for all cameras tested:
                    # Brasil CDN (IP-restricted to Brazil) and EU Camera Door both return 403.
                    # Spending ~1.5s on two failing HTTP calls only delays the RTSP snapshot.
                    # Skip CDN and go straight to snapshot.
                    _LOGGER.debug("Motion %s/%s: signed CDN URL — skipping (known 403), going direct to snapshot", area, name)
                else:
                    _LOGGER.debug(
                        "Motion %s/%s: OSS attempt — bucket=%r path=%r key_len=%d",
                        area, name, ev["bucket"], file_path, len(file_key),
                    )
                    img_bytes = await self._hass.async_add_executor_job(
                        self._camera_api.try_oss_image, ev["bucket"], file_path, file_key
                    )
                    if img_bytes:
                        _LOGGER.debug("Motion %s/%s: OSS image ok (age %.0fs)", area, name, age_s)
                    else:
                        _LOGGER.debug("Motion %s/%s: OSS download returned None", area, name)

        if not img_bytes:
            _LOGGER.debug("Motion %s/%s: no OSS image — trying HA snapshot(s)", area, name)
            cam_entity_id = await self._find_camera_entity_id(dev_id)
            snap_hashes: list[str] = []
            for attempt, delay in enumerate((0, 1, 3)):
                if delay:
                    await asyncio.sleep(delay)
                snap = await self._get_ha_snapshot(dev_id, cam_entity_id)
                if not snap:
                    _LOGGER.debug("Motion %s/%s: snapshot %d/3 failed", area, name, attempt + 1)
                    continue
                snap_hashes.append(hashlib.md5(snap).hexdigest())
                snap_age  = age_s + delay
                snap_note = f"live snapshot ({snap_age:.0f}s after event — person may have left)"
                _LOGGER.debug(
                    "Motion %s/%s: snapshot at +%ds ok (age %.0fs, size=%d bytes, entity=%s)",
                    area, name, delay, snap_age, len(snap), cam_entity_id or "None",
                )
                _dbg = (
                    f"/config/debug_snapshots/snap_{ev_ts.replace(':', '-').replace(' ', '_')}"
                    f"_{area}_{name}_+{delay}s.jpg".replace(" ", "_")
                )
                _snap_copy = snap
                self._hass.async_add_executor_job(
                    lambda p=_dbg, d=_snap_copy: open(p, "wb").write(d)
                )

                if self._ai_client is None:
                    img_bytes = snap
                    break

                result = await self._ai_client.analyze(snap)
                if result is None:
                    # AI unavailable — fail-open with this snapshot
                    img_bytes = snap
                    break
                if result["human"]:
                    img_bytes = snap
                    prefetched_ai = result
                    _LOGGER.debug(
                        "Motion %s/%s: human found at +%ds (conf=%.2f)",
                        area, name, delay, result["confidence"],
                    )
                    break
                if delay == 3:
                    # All attempts exhausted — check for stale RTSP frame before discarding.
                    # Applies universally: Brasil v4.0, Brasil ?param=, Wallis, Camera Door —
                    # any camera that ends up in the snapshot path can hit this.
                    # Threshold ≥2 so a single failed snapshot doesn't mask the stale condition.
                    if len(snap_hashes) >= 2 and len(set(snap_hashes)) == 1:
                        _LOGGER.warning(
                            "Motion %s/%s: all 3 snapshots identical (md5=%s) — RTSP stream serving stale frame",
                            area, name, snap_hashes[0],
                        )
                        await self._send_tech_alert(
                            f"Stale RTSP — {area}/{name}",
                            f"Motion at <b>{name}</b> ({area}) at {ev_ts}.<br><br>"
                            f"{len(snap_hashes)} of 3 snapshots were byte-for-byte identical — "
                            f"RTSP stream is buffering a stale pre-event frame "
                            f"(Tuya token expiry window). Attempting autonomous heal "
                            f"(update_entity → 15 s reconnect → retry snapshot + AI).",
                        )
                        heal_result = await self._try_rtsp_heal(cam_entity_id, dev_id, snap, area, name)
                        if heal_result:
                            img_bytes, prefetched_ai = heal_result
                            snap_note = "recovered snapshot after autonomous RTSP stream heal"
                            break   # exit snapshot loop → fall through to AI filtering + email
                    _LOGGER.debug(
                        "Motion %s/%s: no human at +%ds (conf=%.2f) — all attempts exhausted",
                        area, name, delay, result["confidence"],
                    )
                    if self._ai_stats:
                        await self._ai_stats.async_record(human=False, area=area, camera=name)
                        self._hass.bus.async_fire(EVENT_AI_UPDATED)
                    return
                _LOGGER.debug(
                    "Motion %s/%s: no human at +%ds (conf=%.2f) — retrying",
                    area, name, delay, result["confidence"],
                )
            else:
                _LOGGER.warning(
                    "Motion %s/%s: no image available (OSS failed + all snapshots failed) — event discarded",
                    area, name,
                )
                return

        # ── AI filtering ──────────────────────────────────────────────────────
        email_image    = img_bytes  # may be replaced with annotated image
        detected_label: str | None = None  # set when animal (or human+animal) detected

        if self._ai_client is not None:
            if not img_bytes:
                _LOGGER.warning("Motion %s/%s: AI enabled but no image — event discarded", area, name)
                return

            ai_result = prefetched_ai if prefetched_ai is not None else await self._ai_client.analyze(img_bytes)

            if ai_result is None:
                # Service unreachable — fail-open on human path; animal path stays silent
                _LOGGER.warning("Motion %s/%s: AI service unavailable — failing open", area, name)
            else:
                human_found  = ai_result["human"]
                animal_label = self._check_animal(dev_id, ai_result)

                if not human_found and animal_label is None:
                    _LOGGER.debug(
                        "Motion %s/%s: no human or animal detected (conf=%.2f) — discarding",
                        area, name, ai_result["confidence"],
                    )
                    if self._ai_stats:
                        await self._ai_stats.async_record(human=False, area=area, camera=name)
                        self._hass.bus.async_fire(EVENT_AI_UPDATED)
                    return

                if human_found:
                    _LOGGER.info(
                        "Motion %s/%s: human detected (conf=%.2f) — alerting",
                        area, name, ai_result["confidence"],
                    )
                if animal_label:
                    _LOGGER.info("Motion %s/%s: animal detected (%s) — alerting", area, name, animal_label)

                if self._ai_stats:
                    await self._ai_stats.async_record(human=human_found, area=area, camera=name)
                    self._hass.bus.async_fire(EVENT_AI_UPDATED)
                email_image = ai_result.get("annotated_image", img_bytes)

                if human_found and animal_label:
                    detected_label = f"human + {animal_label}"
                elif animal_label:
                    detected_label = animal_label
                elif human_found:
                    detected_label = "human"
        # ─────────────────────────────────────────────────────────────────────

        snap_row = (
            f'<tr><td><b>Note</b></td><td style="color:#e67e22;">{snap_note}</td></tr>'
            if snap_note else ""
        )
        if detected_label:
            subject = f"{detected_label.capitalize()} detected — {area} / {name} [MQTT]"
        else:
            subject = f"Motion detected — {area} / {name} [MQTT]"
        body    = f"""<html><body>
<h2 style="color:#c0392b;">Motion Detected</h2>
<table>
  <tr><td><b>Camera</b></td><td>{name}</td></tr>
  <tr><td><b>Area</b></td><td>{area}</td></tr>
  <tr><td><b>Time</b></td><td>{ev_ts}</td></tr>
  <tr><td><b>Source</b></td><td style="color:#2980b9;">MQTT (Tuya broker · {self._entry_label})</td></tr>
  {snap_row}
</table>
{'<br><img src="cid:motion_image" style="max-width:640px; border:1px solid #ccc;">' if email_image else ''}
<br><p>Check your recording in the camera app.</p>
</body></html>"""

        to_addrs = self._get_recipients(area, "human")
        if to_addrs:
            if not self._alerts_enabled:
                _LOGGER.debug("Motion %s/%s: MQTT alerts disabled — email suppressed", area, name)
            else:
                await self._hass.async_add_executor_job(
                    self._notifier.send, subject, body, to_addrs, email_image
                )
                _LOGGER.info("Motion alert sent for %s/%s to %s", area, name, to_addrs)
        else:
            _LOGGER.warning(
                "Motion %s/%s: detection fired but no recipients configured for area %r — "
                "email NOT sent. Check the camera's Area assignment and this project's "
                "recipients config.",
                area, name, area,
            )

    def _check_animal(self, dev_id: str, ai_result: dict) -> str | None:
        """Return first matched animal class label if animal detection is enabled for this camera."""
        cam_cfg = self._animal_cfg.get(dev_id, {})
        if not cam_cfg.get("enabled"):
            return None
        detected = ai_result.get("animals", [])
        allowed  = cam_cfg.get("classes", [])
        matches  = [a for a in detected if not allowed or a in allowed]
        return matches[0] if matches else None

    async def _find_camera_entity_id(self, dev_id: str) -> str | None:
        """Look up the HA camera entity ID for a Tuya device ID."""
        try:
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(self._hass)
            for entry in registry.entities.values():
                uid = entry.unique_id or ""
                if entry.entity_id.startswith("camera.") and (
                    uid == f"tuya.{dev_id}" or uid == dev_id
                ):
                    return entry.entity_id
        except Exception:
            pass
        return None

    async def _get_ha_snapshot(self, dev_id: str, entity_id: str | None = None) -> bytes | None:
        """Fetch a live snapshot from the HA camera entity for this Tuya device."""
        try:
            from homeassistant.components.camera import async_get_image
            if entity_id is None:
                entity_id = await self._find_camera_entity_id(dev_id)
            if not entity_id:
                _LOGGER.debug("No HA camera entity found for device %s", dev_id)
                return None
            image = await async_get_image(self._hass, entity_id, timeout=10)
            return image.content
        except Exception as err:
            _LOGGER.warning("HA snapshot failed for device %s (%s): %s", dev_id, entity_id or "?", err)
            return None

    async def _try_rtsp_heal(
        self,
        entity_id: str | None,
        dev_id: str,
        stale_snap: bytes,
        area: str,
        name: str,
    ) -> tuple[bytes, dict | None] | None:
        """Attempt autonomous RTSP recovery after stale-frame detection.

        Calls homeassistant.update_entity to trigger an RTSP URL refresh in go2rtc,
        waits 15s for reconnection, then takes one more snapshot.
        Returns (image_bytes, ai_result) if a human is found, else None.
        """
        if not entity_id:
            _LOGGER.debug("Motion %s/%s: RTSP heal skipped — no camera entity", area, name)
            return None
        try:
            _LOGGER.info(
                "Motion %s/%s: stale RTSP — calling update_entity on %s to force stream refresh",
                area, name, entity_id,
            )
            await self._hass.services.async_call(
                "homeassistant", "update_entity",
                {"entity_id": entity_id},
                blocking=True,
            )
            await asyncio.sleep(15)   # give go2rtc time to reconnect with fresh RTSP URL

            snap = await self._get_ha_snapshot(dev_id, entity_id)
            if not snap:
                _LOGGER.info("Motion %s/%s: RTSP heal snapshot failed", area, name)
                return None

            if hashlib.md5(snap).hexdigest() == hashlib.md5(stale_snap).hexdigest():
                _LOGGER.info("Motion %s/%s: RTSP heal snapshot still identical — stream not recovered yet", area, name)
                return None

            _LOGGER.info("Motion %s/%s: RTSP heal snapshot is fresh — running AI", area, name)
            if self._ai_client is None:
                return (snap, None)   # AI disabled — return image, caller decides

            result = await self._ai_client.analyze(snap)
            if result is None:
                # AI unreachable — fail-open, return image so caller emails it
                return (snap, None)
            if result["human"]:
                _LOGGER.info(
                    "Motion %s/%s: RTSP heal — human detected (conf=%.2f) — sending alert",
                    area, name, result["confidence"],
                )
                return (snap, result)
            _LOGGER.info(
                "Motion %s/%s: RTSP heal — no human in recovered snapshot (conf=%.2f) — discarding",
                area, name, result["confidence"],
            )
            return None
        except Exception as err:
            _LOGGER.debug("Motion %s/%s: RTSP heal attempt failed: %s", area, name, err)
            return None

    def _get_recipients(self, area: str, kind: str) -> list[str]:
        import re
        raw = self._recipients_cfg.get(area, {}).get(kind, "")
        return [r.strip() for r in re.split(r"[;,]", raw) if r.strip()]

    def _get_all_tech_recipients(self) -> list[str]:
        import re
        addrs: set[str] = set()
        for area_cfg in self._recipients_cfg.values():
            raw = area_cfg.get("tech", "")
            for r in re.split(r"[;,]", raw):
                r = r.strip()
                if r:
                    addrs.add(r)
        return list(addrs)

    async def _send_tech_alert(self, subject: str, detail: str) -> None:
        """Send a tech alert email — rate-limited to once per 30 min."""
        now = time.monotonic()
        if now - self._last_tech_alert_at < TECH_ALERT_COOLDOWN:
            _LOGGER.debug("Tech alert suppressed (rate-limit): %s", subject)
            return
        self._last_tech_alert_at = now
        to = self._get_all_tech_recipients()
        if not to:
            _LOGGER.warning("Tech alert '%s' — no tech recipients configured", subject)
            return
        body = (
            f"<html><body>"
            f"<h2 style='color:#c0392b;'>{subject}</h2>"
            f"<p>{detail}</p>"
            f"<p style='color:#888;font-size:0.9em;'>Home Assistant / tuya_cameras</p>"
            f"</body></html>"
        )
        await self._hass.async_add_executor_job(
            self._notifier.send, f"[HA Cameras] {subject}", body, to, None
        )
        _LOGGER.info("Tech alert sent: %s → %s", subject, to)

    def _get_camera_states(self) -> dict[str, str]:
        """Return {camera_name: ha_state} for all cameras known to this bridge's coordinator."""
        result: dict[str, str] = {}
        try:
            cam_data = (self._cam_coord.data or {}).get("cameras", {}) if self._cam_coord else {}
            for dev_id, cam in cam_data.items():
                name = cam.get("name", dev_id)
                entity_id = cam.get("entity_id")
                if not entity_id:
                    # Derive from device_id: official Tuya hub uses tuya. prefix stripped as entity
                    entity_id = f"camera.{dev_id.lower().replace('-', '_')}"
                state = self._hass.states.get(entity_id)
                result[name] = state.state if state else "unknown"
        except Exception:
            pass
        return result

    async def _watchdog(self) -> None:
        """Send a tech alert if MQTT goes silent beyond the expected threshold.

        Two thresholds:
        - Disconnected (client is None or not connected): alert after 1 h silence.
          A healthy bridge reconnects within seconds; 1 h means reconnection is stuck.
        - Connected but silent: alert after 8 h silence.
          Cameras in quiet locations (Wallis farm, overnight) can go hours without
          a motion event — that is normal and should not produce alerts.
        """
        while True:
            try:
                await asyncio.sleep(WATCHDOG_CHECK_S)
                silence = time.monotonic() - self._last_msg_at
                connected = bool(
                    self._current_client and self._current_client.is_connected()
                )
                threshold = WATCHDOG_SILENCE_CONN_S if connected else WATCHDOG_SILENCE_S
                if silence > threshold:
                    # Check whether cameras in this bridge are offline (intentionally disabled).
                    # If all known cameras are unavailable, silence is explained — skip heal.
                    cam_states = self._get_camera_states()
                    all_offline = bool(cam_states) and all(
                        s in ("unavailable", "idle", "off") for s in cam_states.values()
                    )
                    if all_offline:
                        _LOGGER.debug(
                            "Watchdog %s: silence %.0f min but all cameras offline — suppressing",
                            self._entry_label, silence / 60,
                        )
                        await self._send_tech_alert(
                            f"MQTT bridge silent — {self._entry_label} — cameras offline",
                            f"No MQTT messages for {silence / 60:.0f} minutes, but all cameras "
                            f"for this bridge appear offline or unavailable in HA "
                            f"({', '.join(f'{k}={v}' for k, v in cam_states.items())}). "
                            f"Silence is expected. Re-enable cameras to resume motion alerts.",
                        )
                    else:
                        heal_eligible = (
                            connected
                            and (time.monotonic() - self._last_healed_at) > threshold
                        )
                        if heal_eligible:
                            # Force a reconnect: disconnecting the live client triggers on_disconnect
                            # → disconnect_event.set() → _run() reconnects with fresh AF_INET creds.
                            # Fixes IPv6-stale sessions; harmless if silence is genuine.
                            client_to_drop = self._current_client
                            self._current_client = None
                            self._last_healed_at = time.monotonic()
                            self._last_msg_at    = time.monotonic()  # reset window from heal time
                            await self._hass.async_add_executor_job(self._disconnect, client_to_drop)
                            _LOGGER.info(
                                "Watchdog heal: forced reconnect for %s after %.0f min silence",
                                self._entry_label, silence / 60,
                            )
                            await self._send_tech_alert(
                                f"MQTT bridge silent — {self._entry_label} — reconnecting",
                                f"No MQTT messages for {silence / 60:.0f} minutes (connected but silent). "
                                f"Forcing reconnect to heal potential stale session (e.g. IPv6 stuck). "
                                f"If events resume, the session was stale. If still silent after 8 h, "
                                f"cameras in this area may genuinely have no motion.",
                            )
                        else:
                            state = "connected but silent" if connected else "disconnected"
                            await self._send_tech_alert(
                                f"MQTT bridge silent — {self._entry_label}",
                                f"No MQTT messages received for {silence / 60:.0f} minutes "
                                f"(bridge is {state}). "
                                f"A reconnect was already attempted. If no events arrive, this area "
                                f"may be genuinely quiet or the broker has a persistent issue.",
                            )
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.debug("Watchdog loop error: %s", err)

    @staticmethod
    def _decrypt(data: Any, key: bytes) -> dict:
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                from cryptography.hazmat.backends import default_backend
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                raw   = base64.b64decode(data)
                n     = len(raw) - (len(raw) % 16)
                c     = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
                plain = c.decryptor().update(raw[:n])
                pad   = plain[-1]
                if 1 <= pad <= 16:
                    plain = plain[:-pad]
                return json.loads(plain)
            except Exception:
                try:
                    return json.loads(data)
                except Exception:
                    return {}
        return {}

    @classmethod
    def _parse_motion_value(
        cls, code: str, raw: Any,
        mqtt_key: bytes | None = None,
        access_id: str = "",
        access_secret: str = "",
        device_key: str = "",
        event_t: int = 0,
        full_password: str = "",
        product_id: str = "",
        device_uuid: str = "",
    ) -> dict:
        try:
            if code == "movement_detect_pic":
                # Try plain JSON first, then base64-encoded JSON (newer firmware sends b64)
                parsed: dict = {}
                if isinstance(raw, dict):
                    parsed = raw
                elif isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        pass
                    if not parsed.get("bucket"):
                        try:
                            parsed = json.loads(base64.b64decode(raw).decode())
                        except Exception:
                            pass
                _LOGGER.debug("movement_detect_pic bucket=%r files=%r", parsed.get("bucket"), parsed.get("files"))
                return parsed
            ev = json.loads(base64.b64decode(raw).decode())

            # v4.0 new format: files is list of {data, keyId, iv} objects — no bucket field.
            files = ev.get("files", [])
            if files and isinstance(files[0], dict):
                ev["bucket"], ev["files"] = cls._decrypt_v4_files(
                    files, mqtt_key, access_id, access_secret, device_key,
                    event_t, full_password, product_id, device_uuid
                )
            return ev
        except Exception as exc:
            _LOGGER.debug("_parse_motion_value failed code=%s exc=%s", code, exc)
            return {}

    @staticmethod
    def _decrypt_v4_files(
        file_objects: list,
        mqtt_key: bytes | None,
        access_id: str = "",
        access_secret: str = "",
        device_key: str = "",
        event_t: int = 0,
        full_password: str = "",
        product_id: str = "",
        device_uuid: str = "",
    ) -> tuple[str | None, list]:
        """Decrypt v4.0 file blobs → (bucket, [[path, filekey], ...]).

        keyId='default' references a stable platform key. Tries CBC, CTR, and GCM modes
        across all plausible key derivations. GCM uses timestamp as AAD (official SDK pattern).
        """
        import hashlib, hmac as _hmac
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        best_ratio = [0.0]

        def _check(dec: bytes, mode_label: str, label: str) -> str | None:
            """Return plaintext if looks like a path, or JPEG sentinel if JPEG magic."""
            if dec[:2] == b"\xff\xd8":
                _LOGGER.debug("v4 decrypt: JPEG magic (mode=%s)", mode_label)
                return "\xff\xd8JPEG"
            printable = sum(1 for b in dec if 32 <= b < 127)
            ratio = printable / max(len(dec), 1)
            if ratio > best_ratio[0]:
                best_ratio[0] = ratio
            return dec.decode("utf-8", errors="replace") if ratio > 0.80 else None

        def _try(label: str, k: bytes, data: bytes, iv: bytes) -> str | None:
            if len(k) not in (16, 24, 32):
                return None

            # ECB mode (no IV — same as MQTT envelope decryption)
            if len(k) == 16:
                try:
                    ctx = Cipher(algorithms.AES(k), modes.ECB(), backend=default_backend()).decryptor()
                    dec = ctx.update(data) + ctx.finalize()
                    pad = dec[-1]
                    if 1 <= pad <= 16:
                        dec = dec[:-pad]
                    result = _check(dec, "ECB", label)
                    if result:
                        return result
                except Exception:
                    pass

            # CBC mode
            try:
                ctx = Cipher(algorithms.AES(k), modes.CBC(iv), backend=default_backend()).decryptor()
                dec = ctx.update(data) + ctx.finalize()
                pad = dec[-1]
                if 1 <= pad <= 16:
                    dec = dec[:-pad]
                result = _check(dec, "CBC", label)
                if result:
                    return result
            except Exception:
                pass

            # CTR mode (no padding, keystream XOR)
            try:
                ctx = Cipher(algorithms.AES(k), modes.CTR(iv), backend=default_backend()).decryptor()
                dec = ctx.update(data) + ctx.finalize()
                result = _check(dec, "CTR", label)
                if result:
                    return result
            except Exception:
                pass

            # GCM mode: nonce = iv[:12], tag = data[-16:], ciphertext = data[:-16]
            # AAD variants: empty, str(event_t), str(event_t).encode()
            if len(data) > 16 and len(k) == 16:
                nonce = iv[:12]
                ct    = data[:-16]
                tag   = data[-16:]
                for aad_label, aad in [("no_aad", b""), ("aad_t", str(event_t).encode())]:
                    try:
                        aesgcm = AESGCM(k)
                        dec = aesgcm.decrypt(nonce, ct + tag, aad if aad else None)
                        result = _check(dec, f"GCM/{aad_label}", label)
                        if result:
                            return result
                    except Exception:
                        pass

            return None

        def _candidates(data: bytes, iv: bytes) -> str | None:
            combos: list[tuple[str, bytes]] = []
            s  = access_secret
            ai = access_id
            pw = full_password

            if product_id and len(product_id) == 16:
                combos.append(("product_id",                      product_id.encode()))
                combos.append(("md5(product_id)[:16]",
                               hashlib.md5(product_id.encode()).digest()))

            if device_uuid and len(device_uuid) >= 16:
                combos.append(("uuid[:16]",                       device_uuid[:16].encode()))
                combos.append(("md5(uuid)[:16]",
                               hashlib.md5(device_uuid.encode()).digest()))

            if device_key:
                dk = device_key.encode()[:16]
                combos.append(("device_local_key",                dk))
                combos.append(("md5(device_key)[:16]",
                               hashlib.md5(device_key.encode()).digest()))

            if pw and len(pw) >= 24:
                combos += [
                    ("password[:16]",                              pw[:16].encode()),
                    ("password[8:24]",                             pw[8:24].encode()),  # same as mqtt_key
                    ("password[-16:]",                             pw[-16:].encode()),
                    ("password[16:32]",                            pw[16:32].encode()),
                ]
                if len(pw) >= 32:
                    combos.append(("password[0:8]+[24:32]",        (pw[0:8]+pw[24:32]).encode()))
            elif mqtt_key:
                combos.append(("mqtt_password[8:24]",             mqtt_key))

            if ai:
                combos.append(("access_id[:16]",                  ai[:16].encode()))

            if s:
                combos += [
                    ("access_secret[:16]",                         s[:16].encode()),
                    ("access_secret[8:24]",                        s[8:24].encode()),
                    ("access_secret[-16:]",                        s[-16:].encode()),
                ]
                if len(s) >= 32:
                    try:
                        combos += [
                            ("fromhex(secret[:32])",               bytes.fromhex(s[:32])),
                            ("fromhex(secret[8:40])",              bytes.fromhex(s[8:40])),
                        ]
                    except ValueError:
                        pass
                md5s = hashlib.md5(s.encode()).digest()
                md5h = hashlib.md5(s.encode()).hexdigest()
                combos += [
                    ("md5(secret)_raw[:16]",                       md5s),
                    ("md5(secret)_hex[8:24]",                      md5h[8:24].encode()),
                    ("md5(secret)_hex[:16]",                       md5h[:16].encode()),
                    ("sha256(secret)[:16]",                        hashlib.sha256(s.encode()).digest()[:16]),
                ]

            if ai and s:
                for combo_key, combo_label in [
                    (ai + s, "id+secret"),
                    (s + ai, "secret+id"),
                    (ai,     "id_only"),
                ]:
                    md5d = hashlib.md5(combo_key.encode()).digest()
                    md5h = hashlib.md5(combo_key.encode()).hexdigest()
                    combos += [
                        (f"md5({combo_label})_raw[:16]",           md5d),
                        (f"md5({combo_label})_hex[:16]",           md5h[:16].encode()),
                        (f"md5({combo_label})_hex[8:24]",          md5h[8:24].encode()),
                        (f"sha256({combo_label})[:16]",            hashlib.sha256(combo_key.encode()).digest()[:16]),
                        (f"hmac_sha256(secret,{combo_label})[:16]",
                         _hmac.new(s.encode(), combo_key.encode(), hashlib.sha256).digest()[:16]),
                    ]

            for label, k in combos:
                result = _try(label, k, data, iv)
                if result:
                    return result
            return None

        bucket = None
        files  = []
        for obj in file_objects:
            try:
                raw_bytes = bytes.fromhex(obj["data"])
                iv_bytes  = bytes.fromhex(obj["iv"])
                _LOGGER.debug(
                    "v4 file blob: keyId=%r data_len=%d iv=%s data=%s",
                    obj.get("keyId"), len(raw_bytes), obj["iv"], obj["data"]
                )
                best_ratio[0] = 0.0
                plaintext = _candidates(raw_bytes, iv_bytes)
                if not plaintext:
                    _LOGGER.debug(
                        "v4 blob: keyId=%r len=%d — no candidate worked (best ratio=%.2f)",
                        obj.get("keyId"), len(raw_bytes), best_ratio[0]
                    )
                    continue
                if plaintext.startswith("\xff\xd8"):
                    # Inline thumbnail — log and skip OSS path (handled by caller via sentinel)
                    _LOGGER.debug("v4 file blob: inline JPEG detected (%d bytes)", len(plaintext))
                    bucket = "__inline_jpeg__"
                    files.append(plaintext)   # raw bytes as list element
                    continue
                _LOGGER.debug("v4 file blob plaintext: %r", plaintext)
                # Format: "bucket/path/to/file.jpg|filekey"  or  just path
                parts = plaintext.split("|", 1)
                full  = parts[0].strip()
                fkey  = parts[1].strip() if len(parts) > 1 else ""
                segs  = full.split("/", 1)
                bucket = segs[0]
                path   = "/" + segs[1] if len(segs) > 1 else full
                files.append([path, fkey])
            except Exception as exc:
                _LOGGER.debug("v4 file blob decrypt failed: %s", exc)
        return bucket, files
