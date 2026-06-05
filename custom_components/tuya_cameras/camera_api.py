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
            _LOGGER.debug("SD status API failed for %s — resp: %r", device_id, resp)
            return {}

        dps = resp.get("result", [])
        total_kb, used_kb = self._parse_sd_storage(dps)
        if total_kb is None:
            all_codes = [e.get("code") for e in dps if e.get("code")]
            _LOGGER.debug("SD storage DPS not found for %s — all codes: %r", device_id, all_codes)
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
            if entry.get("code") in ("sd_storge", "sd_storage"):
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
            f"https://images.tuyaeu.com/{bucket}{file_path}",
            f"https://{bucket}.oss-eu-central-1.aliyuncs.com{file_path}",
        ]:
            try:
                r = requests.get(url, timeout=10)
                _LOGGER.debug("OSS fetch %s → HTTP %d, %d bytes", url, r.status_code, len(r.content))
                if r.status_code != 200 or not r.content:
                    continue
                data = r.content
                if data[:2] == b"\xff\xd8":
                    _LOGGER.debug("OSS image is plain JPEG")
                    return data
                if file_key:
                    decrypted = self._decrypt_image(data, file_key)
                    if decrypted:
                        _LOGGER.debug("OSS image decrypted ok")
                        return decrypted
                    _LOGGER.debug("OSS decrypt failed — magic=%r key_len=%d", data[:4], len(file_key))
            except Exception as exc:
                _LOGGER.debug("OSS fetch exception for %s: %s", url, exc)
                continue
        return None

    @staticmethod
    def _decrypt_image(data: bytes, file_key: str) -> bytes | None:
        # Uses `cryptography` (already in requirements) — NOT pycryptodome.
        # Two formats seen in the wild:
        #   ECB: raw AES-128-ECB ciphertext, PKCS#7 padded, no header
        #   CBC: 4-byte version + 16-byte IV + 44-byte metadata + AES-128-CBC ciphertext
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            raw_key = (
                bytes.fromhex(file_key) if len(file_key) == 32
                else (file_key.encode() + b"\x00" * 16)[:16]
            )

            # Try ECB (older cameras, most common)
            try:
                ctx = Cipher(algorithms.AES(raw_key), modes.ECB(), backend=default_backend()).decryptor()
                dec = ctx.update(data) + ctx.finalize()
                pad = dec[-1]
                if 1 <= pad <= 16:
                    dec = dec[:-pad]
                if dec[:2] == b"\xff\xd8":
                    return dec
            except Exception:
                pass

            # Try CBC with 64-byte header: 4-byte version + 16-byte IV + 44-byte metadata
            if len(data) > 64:
                iv  = data[4:20]
                enc = data[64:]
                try:
                    ctx = Cipher(algorithms.AES(raw_key), modes.CBC(iv), backend=default_backend()).decryptor()
                    dec = ctx.update(enc) + ctx.finalize()
                    pad = dec[-1]
                    if 1 <= pad <= 16:
                        dec = dec[:-pad]
                    if dec[:2] == b"\xff\xd8":
                        return dec
                except Exception:
                    pass

            return None
        except Exception:
            return None
