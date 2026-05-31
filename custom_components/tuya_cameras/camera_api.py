"""Tuya camera API — SD status, format, image download."""

from __future__ import annotations

import base64
import logging
from typing import Any

import requests

from .const import (
    CAMERA_CATEGORIES,
    CONF_HA_TOKEN,
    CONF_HA_URL,
    SD_STATUS_LABELS,
)

_LOGGER = logging.getLogger(__name__)


class CameraAPI:
    """
    Wraps Tuya Cloud calls for camera management.
    Uses the authenticated client from tuya_home_core.
    NEVER calls gw.status() — see CLAUDE.md.
    """

    def __init__(self, tuya_client: Any, ha_config: dict | None = None) -> None:
        self._client    = tuya_client
        self._ha_token  = (ha_config or {}).get(CONF_HA_TOKEN, "")
        self._ha_url    = (ha_config or {}).get(CONF_HA_URL, "http://localhost:8123")

    # ── Camera list from core device data ────────────────────────────────────

    def cameras_from_devices(
        self,
        devices: list[dict],
        area_map: dict[str, str],
    ) -> list[dict]:
        """Filter Tuya device list to cameras and attach area names."""
        cams = []
        for d in devices:
            if d.get("category", "").lower() not in CAMERA_CATEGORIES:
                continue
            dev_id = d["id"]
            cams.append({
                "id":      dev_id,
                "name":    d.get("name", dev_id).strip(),
                "area":    area_map.get(dev_id, ""),
                "online":  d.get("online", False),
            })
        return cams

    # ── SD card status ────────────────────────────────────────────────────────

    def get_sd_status(self, device_id: str) -> dict:
        """
        Poll SD card status for one camera.
        Returns dict with sd_pct, sd_used_gb, sd_total_gb, sd_status, sd_free_gb.
        """
        try:
            resp = self._client.cloudrequest(f"/v1.0/devices/{device_id}/status")
        except Exception as err:
            _LOGGER.warning("SD status fetch failed for %s: %s", device_id, err)
            return {}

        if not (resp and resp.get("success")):
            return {}

        dps = resp.get("result", [])
        total_kb, used_kb = self._parse_sd_storage(dps)
        sd_code = str(self._find_dps(dps, "sd_status") or "")
        sd_status = SD_STATUS_LABELS.get(sd_code, sd_code or "unknown")

        result: dict[str, Any] = {"sd_status": sd_status}

        if total_kb and total_kb > 0:
            used_kb = used_kb or 0
            pct = round(100.0 * used_kb / total_kb, 1)
            result.update({
                "sd_pct":      pct,
                "sd_used_gb":  round(used_kb  / (1024 * 1024), 1),
                "sd_total_gb": round(total_kb  / (1024 * 1024), 1),
                "sd_free_gb":  round((total_kb - used_kb) / (1024 * 1024), 1),
            })
        else:
            result.update({"sd_pct": None, "sd_used_gb": None,
                           "sd_total_gb": None, "sd_free_gb": None})
        return result

    @staticmethod
    def _find_dps(dps: list, code: str) -> Any:
        for entry in dps:
            if entry.get("code") == code:
                return entry.get("value")
        return None

    @staticmethod
    def _parse_sd_storage(dps: list) -> tuple[int | None, int | None]:
        for entry in dps:
            if entry.get("code") == "sd_storge":
                try:
                    parts = str(entry["value"]).split("|")
                    return int(parts[0]), int(parts[1])
                except (IndexError, ValueError):
                    pass
        return None, None

    # ── SD card format ────────────────────────────────────────────────────────

    def format_sd(self, device_id: str) -> bool:
        """Format SD card — tries IPC endpoint first, falls back to device commands."""
        try:
            resp = self._client.cloudrequest(
                f"/v1.0/ipc/{device_id}/sdcard/format", action="POST", post={}
            )
            if resp and resp.get("success"):
                _LOGGER.info("SD format accepted (IPC) for %s", device_id)
                return True
        except Exception as err:
            _LOGGER.debug("IPC format endpoint failed: %s", err)

        try:
            resp = self._client.cloudrequest(
                f"/v1.0/devices/{device_id}/commands",
                action="POST",
                post={"commands": [{"code": "sd_format", "value": True}]},
            )
            if resp and resp.get("success"):
                _LOGGER.info("SD format accepted (commands) for %s", device_id)
                return True
        except Exception as err:
            _LOGGER.warning("SD format failed for %s: %s", device_id, err)

        return False

    # ── Motion image retrieval ────────────────────────────────────────────────

    def try_oss_image(self, bucket: str, file_path: str, file_key: str = "") -> bytes | None:
        """Download motion thumbnail from Tuya OSS. Returns JPEG bytes or None."""
        if not bucket or not file_path:
            return None

        for url in [
            f"https://images.tuyaeu.com{file_path}",
            f"https://{bucket}.oss-eu-central-1.aliyuncs.com{file_path}",
        ]:
            try:
                r = requests.get(url, timeout=10)
                if r.status_code != 200 or not r.content:
                    continue
                data = r.content
                if data[:2] == b"\xff\xd8":
                    return data
                if file_key:
                    decrypted = self._decrypt_image(data, file_key)
                    if decrypted:
                        return decrypted
            except Exception:
                continue
        return None

    @staticmethod
    def _decrypt_image(data: bytes, file_key: str) -> bytes | None:
        try:
            from Crypto.Cipher import AES
            raw_key = (
                bytes.fromhex(file_key) if len(file_key) == 32
                else (file_key.encode() + b"\x00" * 16)[:16]
            )
            dec = AES.new(raw_key, AES.MODE_ECB).decrypt(data)
            pad = dec[-1]
            if 1 <= pad <= 16:
                dec = dec[:-pad]
            return dec if dec[:2] == b"\xff\xd8" else None
        except Exception:
            return None

    def get_ha_snapshot(self, entity_id: str) -> bytes | None:
        """Fetch live camera snapshot from HA REST API."""
        if not self._ha_token or not entity_id:
            return None
        try:
            r = requests.get(
                f"{self._ha_url}/api/camera_proxy/{entity_id}",
                headers={"Authorization": f"Bearer {self._ha_token}"},
                timeout=15,
            )
            if r.status_code == 200 and r.content:
                return r.content
        except Exception as err:
            _LOGGER.debug("HA snapshot failed for %s: %s", entity_id, err)
        return None

    def get_motion_events(
        self, device_id: str, start_ms: int, end_ms: int
    ) -> list[dict]:
        """Retrieve recent motion events via device logs."""
        try:
            result = self._client.getdevicelog(
                device_id, start=start_ms, end=end_ms, size=50
            )
        except Exception as err:
            _LOGGER.debug("getdevicelog error for %s: %s", device_id, err)
            return []

        logs = (result or {}).get("result", {}).get("logs", [])
        events = []
        for log in logs:
            code = log.get("code", "")
            if code not in ("initiative_message", "movement_detect_pic"):
                continue
            raw = log.get("value", "")
            data = self._decode_event(raw)
            if data is None:
                continue
            if code == "initiative_message" and data.get("cmd") != "ipc_motion":
                continue
            files     = data.get("files", [[]])
            file_path = files[0][0] if files and files[0] else ""
            file_key  = files[0][1] if files and files[0] and len(files[0]) > 1 else ""
            events.append({
                "alarm_time": log.get("event_time", 0),
                "bucket":     data.get("bucket", ""),
                "file_path":  file_path,
                "file_key":   file_key,
            })
        return events

    @staticmethod
    def _decode_event(raw: str) -> dict | None:
        import json
        try:
            return json.loads(raw)
        except Exception:
            pass
        try:
            return json.loads(base64.b64decode(raw + "==").decode("utf-8"))
        except Exception:
            return None
