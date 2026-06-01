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
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .ai_client import AIClient
from .ai_stats import AIStats
from .camera_api import CameraAPI
from .const import EVENT_AI_UPDATED
from .notify import Notifier

_LOGGER = logging.getLogger(__name__)

RECONNECT_BUFFER_S = 600   # refresh creds 10 min before expiry
RETRY_DELAY_S      = 30


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
        ai_client: AIClient | None = None,
        ai_stats: AIStats | None = None,
    ) -> None:
        self._hass           = hass
        self._tuya_client    = tuya_client
        self._camera_api     = camera_api
        self._notifier       = notifier
        self._recipients_cfg = recipients_cfg  # {area: {human: "...", tech: "..."}}
        self._uid            = uid
        self._access_id      = access_id
        self._ai_client      = ai_client   # None = AI disabled
        self._ai_stats       = ai_stats
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, hass: Any) -> None:
        self._task = hass.async_create_background_task(
            self._run(), "tuya_cameras_mqtt_bridge"
        )

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        _LOGGER.info("Tuya MQTT bridge starting")
        reconnect_delay = 5  # seconds between reconnects after a drop
        while True:
            try:
                creds = await self._hass.async_add_executor_job(self._fetch_creds)
                if not creds:
                    _LOGGER.error("MQTT credentials unavailable — retrying in %ds", RETRY_DELAY_S)
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue

                expire_s   = creds.get("expire_time", 7200)
                refresh_at = time.monotonic() + expire_s - RECONNECT_BUFFER_S
                key        = creds["password"][8:24].encode()   # AES-128-ECB session key

                loop = asyncio.get_event_loop()
                disconnect_event = asyncio.Event()

                client = await self._hass.async_add_executor_job(self._connect, creds)
                if client is None:
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue

                def on_message(c, userdata, msg):
                    asyncio.run_coroutine_threadsafe(
                        self._handle(msg.payload, key), loop
                    )

                def on_disconnect(c, userdata, *args):
                    _LOGGER.warning("MQTT disconnected — will reconnect in %ds", reconnect_delay)
                    loop.call_soon_threadsafe(disconnect_event.set)

                client.on_message    = on_message
                client.on_disconnect = on_disconnect

                _LOGGER.info("MQTT bridge connected. Creds expire in %ds", expire_s)

                # Wait for disconnect signal or credential expiry
                cred_timeout = refresh_at - time.monotonic()
                try:
                    await asyncio.wait_for(disconnect_event.wait(), timeout=cred_timeout)
                    # Disconnected — wait briefly then reconnect with same creds
                    await self._hass.async_add_executor_job(self._disconnect, client)
                    await asyncio.sleep(reconnect_delay)
                    continue
                except asyncio.TimeoutError:
                    pass  # Credentials about to expire — fall through to refresh

                await self._hass.async_add_executor_job(self._disconnect, client)
                _LOGGER.info("Refreshing MQTT credentials")

            except asyncio.CancelledError:
                _LOGGER.info("MQTT bridge stopped")
                return
            except Exception as err:
                _LOGGER.error("MQTT bridge error: %s — retrying in %ds", err, RETRY_DELAY_S)
                await asyncio.sleep(RETRY_DELAY_S)

    # ── Connection helpers (blocking — called via executor) ───────────────────

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
                return r["result"]
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
            client.connect(host, port, keepalive=60)
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

    async def _handle(self, payload: bytes, key: bytes) -> None:
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

        motion_dps = [s for s in status if s.get("code") in ("initiative_message", "movement_detect_pic")]
        if not motion_dps:
            return

        core_data = self._hass.data.get("tuya_home_core", {})
        cameras: dict = {}
        for entry_data in core_data.values():
            coord = entry_data.get("coordinator")
            if coord and coord.data:
                cam_api_data = self._camera_api.cameras_from_devices(
                    coord.data.get("devices", []),
                    coord.data.get("areas", {}),
                )
                cameras = {c["id"]: c for c in cam_api_data}
                break

        cam = cameras.get(dev_id)
        if not cam:
            _LOGGER.debug("Motion from unknown device %s — skipping", dev_id)
            return

        area = cam.get("area", "Unknown")
        name = cam["name"]

        newest    = max(motion_dps, key=lambda s: s.get("t", 0))
        t_ms      = newest.get("t", int(time.time())) * 1000
        raw_v     = newest.get("value", "")
        ev        = self._parse_motion_value(newest["code"], raw_v)
        age_s     = (time.time() * 1000 - t_ms) / 1000

        img_bytes = None
        snap_note = ""

        if ev.get("bucket") and ev.get("files"):
            parts     = ev["files"][0] if ev.get("files") else []
            file_path = parts[0] if len(parts) > 0 else ""
            file_key  = parts[1] if len(parts) > 1 else ""
            img_bytes = await self._hass.async_add_executor_job(
                self._camera_api.try_oss_image, ev["bucket"], file_path, file_key
            )
            if img_bytes:
                _LOGGER.debug("Motion %s/%s: OSS image ok (age %.0fs)", area, name, age_s)

        if not img_bytes:
            _LOGGER.debug("Motion %s/%s: no OSS image", area, name)

        ev_ts = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # ── AI filtering ──────────────────────────────────────────────────────
        email_image = img_bytes  # may be replaced with annotated image

        if self._ai_client is not None:
            if not img_bytes:
                _LOGGER.debug("Motion %s/%s: AI enabled, no image — discarding", area, name)
                return

            ai_result = await self._ai_client.analyze(img_bytes)

            if ai_result is None:
                # Service unreachable or timed out — fail-open, email original image
                _LOGGER.warning("Motion %s/%s: AI service unavailable — failing open", area, name)
            elif not ai_result["human"]:
                _LOGGER.debug(
                    "Motion %s/%s: no human detected (conf=%.2f) — discarding",
                    area, name, ai_result["confidence"],
                )
                if self._ai_stats:
                    await self._ai_stats.async_record(human=False, area=area, camera=name)
                    self._hass.bus.async_fire(EVENT_AI_UPDATED)
                return
            else:
                _LOGGER.info(
                    "Motion %s/%s: human detected (conf=%.2f) — alerting",
                    area, name, ai_result["confidence"],
                )
                if self._ai_stats:
                    await self._ai_stats.async_record(human=True, area=area, camera=name)
                    self._hass.bus.async_fire(EVENT_AI_UPDATED)
                email_image = ai_result.get("annotated_image", img_bytes)
        # ─────────────────────────────────────────────────────────────────────

        snap_row = (
            f'<tr><td><b>Note</b></td><td style="color:#e67e22;">{snap_note}</td></tr>'
            if snap_note else ""
        )
        subject = f"Motion detected — {area} / {name}"
        body    = f"""<html><body>
<h2 style="color:#c0392b;">Motion Detected</h2>
<table>
  <tr><td><b>Camera</b></td><td>{name}</td></tr>
  <tr><td><b>Area</b></td><td>{area}</td></tr>
  <tr><td><b>Time</b></td><td>{ev_ts}</td></tr>
  {snap_row}
</table>
{'<br><img src="cid:motion_image" style="max-width:640px; border:1px solid #ccc;">' if email_image else ''}
<br><p>Check your recording in the camera app.</p>
</body></html>"""

        to_addrs = self._get_recipients(area, "human")
        if to_addrs:
            await self._hass.async_add_executor_job(
                self._notifier.send, subject, body, to_addrs, email_image
            )
            _LOGGER.info("Motion alert sent for %s/%s to %s", area, name, to_addrs)

    def _get_recipients(self, area: str, kind: str) -> list[str]:
        import re
        raw = self._recipients_cfg.get(area, {}).get(kind, "")
        return [r.strip() for r in re.split(r"[;,]", raw) if r.strip()]

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

    @staticmethod
    def _parse_motion_value(code: str, raw: Any) -> dict:
        try:
            if code == "movement_detect_pic":
                return json.loads(raw) if isinstance(raw, str) else raw
            return json.loads(base64.b64decode(raw).decode())
        except Exception:
            return {}
