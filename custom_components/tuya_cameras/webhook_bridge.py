"""SmartLife notification bridge — receives multipart POST from the Android
notification listener app and routes motion images through the AI+email pipeline."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .const import DOMAIN, WEBHOOK_ID
from .notify_helper import send_email

_LOGGER = logging.getLogger(__name__)


def _check_animal_cfg(animal_cfg: dict, dev_id: str | None, ai_result: dict | None) -> str | None:
    """Return first matched animal class label if animal detection is enabled for this camera."""
    if not dev_id or not ai_result:
        return None
    cam_cfg = animal_cfg.get(dev_id, {})
    if not cam_cfg.get("enabled"):
        return None
    detected = ai_result.get("animals", [])
    allowed  = cam_cfg.get("classes", [])
    matches  = [a for a in detected if not allowed or a in allowed]
    return matches[0] if matches else None


class SmartLifeWebhookBridge:
    def __init__(self, hass: Any) -> None:
        self._hass = hass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        from homeassistant.components import webhook
        webhook.async_register(
            self._hass, DOMAIN, "SmartLife Motion Bridge",
            WEBHOOK_ID, self._handle,
        )
        _LOGGER.info("SmartLife webhook bridge registered — POST to /api/webhook/%s", WEBHOOK_ID)

    def stop(self) -> None:
        from homeassistant.components import webhook
        try:
            webhook.async_unregister(self._hass, WEBHOOK_ID)
        except Exception:
            pass
        _LOGGER.info("SmartLife webhook bridge unregistered")

    # ── Request handler ───────────────────────────────────────────────────────

    async def _handle(self, hass: Any, webhook_id: str, request: Any) -> None:
        # Read the multipart body synchronously here — the HTTP connection must
        # stay open until we've consumed the body, but we respond immediately
        # after and do all heavy work (snapshots, AI, email) in a background task.
        # This prevents the Android bridge's 10s readTimeout from firing while
        # HA is still taking RTSP snapshots.
        try:
            data = await request.post()
        except Exception as err:
            _LOGGER.error("SmartLife webhook: failed to parse POST body: %s", err)
            return

        title       = data.get("title", "")
        text        = data.get("text", "")
        image_field = data.get("image")

        img_bytes: bytes | None = None
        if image_field and hasattr(image_field, "file"):
            img_bytes = image_field.file.read()

        _LOGGER.info(
            "SmartLife webhook: title=%r text=%r image=%s",
            title, text, f"{len(img_bytes)}B" if img_bytes else "none",
        )

        # Respond 200 immediately — heavy processing runs in background.
        hass.async_create_task(self._process(hass, title, text, img_bytes))

    async def _process(self, hass: Any, title: str, text: str, img_bytes: bytes | None) -> None:
        area, cam_name, entry_data, dev_id = self._find_camera(title, text)

        # Respect the per-entry webhook toggle
        if entry_data and not entry_data.get("webhook_alerts_enabled", False):
            _LOGGER.debug(
                "SmartLife webhook %s/%s: webhook alerts disabled for matched entry — skipping",
                area, cam_name,
            )
            return

        _LOGGER.debug("SmartLife webhook: matched area=%r camera=%r", area, cam_name)

        bridge   = entry_data.get("bridge")    if entry_data else None
        ai_stats = entry_data.get("ai_stats")  if entry_data else None

        ai_client  = bridge._ai_client if bridge else self._first_ai_client()
        recipients = (
            bridge._get_recipients(area, "human")
            if bridge else self._all_human_recipients()
        )
        animal_cfg     = entry_data.get("animal_cfg", {}) if entry_data else {}
        detected_label: str | None = None

        # ── No image — fall back to RTSP snapshots ────────────────────────────
        # Camera Door (and potentially other cameras) uses a plain notification
        # style with no BigPicture. Take up to 3 HA snapshots at t=0/+2s/+4s,
        # same as the MQTT path.
        if not img_bytes:
            _LOGGER.debug(
                "SmartLife webhook %s/%s: no image in notification — falling back to RTSP snapshots",
                area, cam_name,
            )
            if not bridge:
                _LOGGER.warning(
                    "SmartLife webhook %s/%s: no image and no bridge — cannot take snapshot, discarding",
                    area, cam_name,
                )
                return
            entity_id = await bridge._find_camera_entity_id(dev_id) if dev_id else None
            for attempt, delay in enumerate([0, 1, 3]):
                if delay:
                    await asyncio.sleep(delay)
                snap = await bridge._get_ha_snapshot(dev_id, entity_id) if dev_id else None
                if snap is None:
                    _LOGGER.debug(
                        "SmartLife webhook %s/%s: RTSP snapshot %d/3 failed",
                        area, cam_name, attempt + 1,
                    )
                    continue
                if ai_client is not None:
                    result = await ai_client.analyze(snap)
                    if result and result["human"]:
                        _LOGGER.info(
                            "SmartLife webhook %s/%s: human found in RTSP snapshot at +%ds (conf=%.2f)",
                            area, cam_name, delay, result["confidence"],
                        )
                        if ai_stats:
                            await ai_stats.async_record(human=True, area=area, camera=cam_name)
                            self._hass.bus.async_fire(f"{DOMAIN}_ai_updated")
                        img_bytes      = result.get("annotated_image", snap)
                        detected_label = "human"
                        break
                    animal = _check_animal_cfg(animal_cfg, dev_id, result)
                    if animal:
                        _LOGGER.info(
                            "SmartLife webhook %s/%s: animal (%s) found in RTSP snapshot at +%ds",
                            area, cam_name, animal, delay,
                        )
                        if ai_stats:
                            await ai_stats.async_record(human=False, area=area, camera=cam_name)
                            self._hass.bus.async_fire(f"{DOMAIN}_ai_updated")
                        img_bytes = result.get("annotated_image", snap)
                        detected_label = animal
                        break
                    _LOGGER.debug(
                        "SmartLife webhook %s/%s: no human or animal in RTSP snapshot at +%ds (conf=%.2f)",
                        area, cam_name, delay, result["confidence"] if result else 0.0,
                    )
                else:
                    # No AI — use first available snapshot and send
                    img_bytes = snap
                    break
            if not img_bytes:
                _LOGGER.debug(
                    "SmartLife webhook %s/%s: no human or animal found in any RTSP snapshot — discarding",
                    area, cam_name,
                )
                if ai_stats:
                    await ai_stats.async_record(human=False, area=area, camera=cam_name)
                    self._hass.bus.async_fire(f"{DOMAIN}_ai_updated")
                return

        # ── AI filtering (image path) ─────────────────────────────────────────
        elif ai_client is not None:
            result = await ai_client.analyze(img_bytes)
            if result is None:
                _LOGGER.warning(
                    "SmartLife webhook %s/%s: AI service unavailable — failing open", area, cam_name
                )
            elif not result["human"]:
                animal = _check_animal_cfg(animal_cfg, dev_id, result)
                if animal is None:
                    _LOGGER.debug(
                        "SmartLife webhook %s/%s: no human or animal detected (conf=%.2f) — discarding",
                        area, cam_name, result["confidence"],
                    )
                    if ai_stats:
                        await ai_stats.async_record(human=False, area=area, camera=cam_name)
                        self._hass.bus.async_fire(f"{DOMAIN}_ai_updated")
                    return
                _LOGGER.info("SmartLife webhook %s/%s: animal detected (%s) — alerting", area, cam_name, animal)
                if ai_stats:
                    await ai_stats.async_record(human=False, area=area, camera=cam_name)
                    self._hass.bus.async_fire(f"{DOMAIN}_ai_updated")
                img_bytes      = result.get("annotated_image", img_bytes)
                detected_label = animal
            else:
                _LOGGER.info(
                    "SmartLife webhook %s/%s: human detected (conf=%.2f) — alerting",
                    area, cam_name, result["confidence"],
                )
                if ai_stats:
                    await ai_stats.async_record(human=True, area=area, camera=cam_name)
                    self._hass.bus.async_fire(f"{DOMAIN}_ai_updated")
                img_bytes      = result.get("annotated_image", img_bytes)
                detected_label = "human"

        if not recipients:
            _LOGGER.warning(
                "SmartLife webhook %s/%s: no recipients configured — skipping email", area, cam_name
            )
            return

        ev_ts   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if detected_label:
            subject = f"{detected_label.capitalize()} detected — {area} / {cam_name} [SmartLife]"
        else:
            subject = f"Motion detected — {area} / {cam_name} [SmartLife]"
        body    = (
            f"<html><body>"
            f"<h2 style='color:#c0392b;'>Motion Detected</h2>"
            f"<table>"
            f"<tr><td><b>Camera</b></td><td>{cam_name}</td></tr>"
            f"<tr><td><b>Area</b></td><td>{area}</td></tr>"
            f"<tr><td><b>Time</b></td><td>{ev_ts}</td></tr>"
            f"<tr><td><b>Source</b></td><td style='color:#27ae60;'>"
            f"SmartLife capture (tablet bridge)</td></tr>"
            f"</table>"
            f"<br><img src='cid:motion_image' style='max-width:640px; border:1px solid #ccc;'>"
            f"<br><p>Check your recording in the camera app.</p>"
            f"</body></html>"
        )

        await send_email(self._hass, subject, body, recipients, img_bytes)
        _LOGGER.info(
            "SmartLife webhook alert sent for %s/%s to %s", area, cam_name, recipients
        )

    # ── Camera lookup ─────────────────────────────────────────────────────────

    # Words too generic to use for partial matching (appear in both camera names and notification text)
    _GENERIC_WORDS = {"camera", "smart", "motion", "detected", "detect", "alert", "cam"}

    def _find_camera(self, title: str, text: str) -> tuple[str, str, dict | None, str | None]:
        """Match notification title/text against known camera names.

        Two-pass strategy:
        1. Exact: full camera name appears anywhere in title+text (case-insensitive)
        2. Partial: a distinctive word (>4 chars, not generic) from the name appears in title+text
        Falls back to title as camera name if nothing matches.
        Returns (area, cam_name, entry_data, dev_id).
        """
        search = f"{title} {text}".lower()
        all_entries = self._all_entry_data()

        def _cameras_for(entry_data: dict):
            # Prefer cameras coordinator — has entity registry fallback when API is down
            cam_coord = entry_data.get("coordinator")
            if cam_coord and cam_coord.data:
                cam_map = cam_coord.data.get("cameras", {})
                if cam_map:
                    return list(cam_map.values())
            core_coord = entry_data.get("core_coord")
            camera_api = entry_data.get("camera_api")
            if not (core_coord and core_coord.data and camera_api):
                return []
            return camera_api.cameras_from_devices(
                core_coord.data.get("devices", []),
                core_coord.data.get("areas", {}),
            )

        # Pass 1 — exact full name match
        for entry_data in all_entries:
            for cam in _cameras_for(entry_data):
                if cam["name"].lower() in search:
                    return cam.get("area", "Unknown"), cam["name"], entry_data, cam.get("id")

        # Pass 2 — distinctive word match (skip generic words)
        for entry_data in all_entries:
            for cam in _cameras_for(entry_data):
                distinctive = [
                    p for p in cam["name"].lower().split()
                    if len(p) > 4 and p not in self._GENERIC_WORDS
                ]
                if distinctive and any(p in search for p in distinctive):
                    return cam.get("area", "Unknown"), cam["name"], entry_data, cam.get("id")

        fallback_name = title or text or "Unknown Camera"
        return "SmartLife", fallback_name, all_entries[0] if all_entries else None, None

    # ── Entry data helpers ────────────────────────────────────────────────────

    def _all_entry_data(self) -> list[dict]:
        return [
            v for v in self._hass.data.get(DOMAIN, {}).values()
            if isinstance(v, dict) and "bridge" in v
        ]

    def _first_ai_client(self) -> Any | None:
        for d in self._all_entry_data():
            b = d.get("bridge")
            if b and b._ai_client:
                return b._ai_client
        return None

    def _all_human_recipients(self) -> list[str]:
        addrs: set[str] = set()
        for d in self._all_entry_data():
            b = d.get("bridge")
            if b:
                for area_cfg in b._recipients_cfg.values():
                    addrs.update(area_cfg.get("human", []))
        return list(addrs)
