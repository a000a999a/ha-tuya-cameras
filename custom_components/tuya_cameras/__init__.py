"""Tuya Cameras — SD monitoring and real-time motion alerts."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .ai_client import AIClient
from .ai_stats import AIStats
from .camera_api import CameraAPI
from .const import (
    CONF_AI_ENABLED, CONF_AI_URL,
    CONF_CAMERA_ANIMAL_CONFIG,
    CONF_CORE_ENTRY_ID, CONF_RECIPIENTS, CONF_REFRESH_DAYS, DEFAULT_REFRESH_DAYS,
    CONF_MQTT_ALERTS_ENABLED, CONF_WEBHOOK_ALERTS_ENABLED,
    CONF_SMTP_HOST, CONF_SMTP_PASSWORD, CONF_SMTP_PORT, CONF_SMTP_SENDER,
    DOMAIN, DOMAIN_CORE,
)
from .coordinator import CameraCoordinator
from .mqtt_bridge import TuyaMQTTBridge
from .notify import Notifier
from .webhook_bridge import SmartLifeWebhookBridge

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "button"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register domain-wide services (called once regardless of how many entries exist)."""

    async def _handle_refresh_all(call: ServiceCall) -> None:
        """
        Refresh every tuya_cameras entry then update the Lovelace Cameras view.

        If new cameras are discovered in any entry (not present before the refresh),
        that entry is reloaded so HA creates sensor/button entities for the new
        cameras. A short wait is inserted before the Lovelace update so the entity
        registry has time to settle after the reload.
        """
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            _LOGGER.warning("refresh_all: no tuya_cameras entries loaded")
            return

        new_cameras_detected = False

        for entry_id, data in list(entries.items()):
            if not isinstance(data, dict):
                continue
            core_coord = data.get("core_coord")
            cam_coord  = data.get("coordinator")

            cameras_before = set((cam_coord.data or {}).get("cameras", {}).keys()) if cam_coord else set()

            try:
                if core_coord:
                    await core_coord.async_refresh()
                if cam_coord:
                    await cam_coord.async_refresh()
            except Exception as err:
                _LOGGER.error("refresh_all: error refreshing entry %s: %s", entry_id, err)
                continue

            cameras_after = set((cam_coord.data or {}).get("cameras", {}).keys()) if cam_coord else set()
            new     = cameras_after - cameras_before
            removed = cameras_before - cameras_after
            if new:
                new_cameras_detected = True
                _LOGGER.info(
                    "refresh_all: %d new camera(s) in entry %s — reloading to create entities: %s",
                    len(new), entry_id, new,
                )
                hass.async_create_task(hass.config_entries.async_reload(entry_id))
            if removed:
                registry = er.async_get(hass)
                for entity in list(registry.entities.values()):
                    if entity.config_entry_id != entry_id or entity.platform != DOMAIN:
                        continue
                    parts = entity.unique_id.split("_")
                    if len(parts) >= 2 and parts[1] in removed:
                        _LOGGER.info("refresh_all: removing stale entity %s (device removed from account)", entity.entity_id)
                        registry.async_remove(entity.entity_id)

        _LOGGER.info("refresh_all: done (%d entries refreshed)", sum(1 for v in entries.values() if isinstance(v, dict)))

        if new_cameras_detected:
            # Allow entity registration to settle after config entry reload
            await asyncio.sleep(5)

        await _update_lovelace_views(hass)

    hass.services.async_register(DOMAIN, "refresh_all", _handle_refresh_all)
    return True


async def _update_lovelace_views(hass: HomeAssistant) -> None:
    """
    Update both the Cameras view and the AI Detections view in the Overview dashboard.

    Cameras view:
    - Updates entity IDs in existing picture-glance cards.
    - Adds a new picture-glance card for every camera that has no card yet,
      inserting it into the section that already contains other cameras from
      the same area (or a new section if none exists).
    - Removes cards for cameras that no longer exist in any coordinator.
    AI Detections view: rebuilds Last Human Detection tiles for all known areas;
      adds missing Live Counts / 7-Day Summary stats for new tuya_cameras entries.
    """
    registry    = er.async_get(hass)
    domain_data = hass.data.get(DOMAIN, {})

    # Build: lowercase camera name → {entity IDs, project, camera entity_id}
    cam_info: dict[str, dict] = {}
    for entry_id, data in domain_data.items():
        if not isinstance(data, dict) or "coordinator" not in data:
            continue
        config_entry = hass.config_entries.async_get_entry(entry_id)
        # tuya_cameras entry title always inherits its linked tuya_home_core
        # project's title (see config_flow.py _create()) — use it as the
        # grouping key instead of HA Area, which is per-device, manually
        # assigned, and empty for any camera not yet touched in Settings.
        project = config_entry.title if config_entry else entry_id
        cam_data = (data.get("coordinator").data or {}).get("cameras", {})
        for dev_id, cam in cam_data.items():
            name = cam.get("name", "").strip()
            area = cam.get("area", "").strip()
            if not name:
                continue
            sd_eid     = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_sd_pct")
            online_eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_online")
            fmt_eid    = registry.async_get_entity_id("button", DOMAIN, f"{entry_id}_{dev_id}_format_sd")
            animal_eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_animal")
            animal_enabled = data.get("animal_cfg", {}).get(dev_id, {}).get("enabled", False)
            # Find the official HA camera entity (created by the Tuya integration)
            cam_entity = None
            for entry in registry.entities.values():
                uid = entry.unique_id or ""
                if entry.entity_id.startswith("camera.") and (
                    uid == f"tuya.{dev_id}" or uid == dev_id
                ):
                    cam_entity = entry.entity_id
                    break
            if sd_eid or online_eid:
                name_key = name.lower()
                existing_animal = cam_info.get(name_key, {}).get("animal")
                cam_info[name_key] = {
                    "sd": sd_eid, "online": online_eid, "fmt": fmt_eid,
                    "animal": (animal_eid if animal_enabled else None) or existing_animal,
                    "area": area, "area_missing": not area, "dev_id": dev_id,
                    "project": project, "camera_entity": cam_entity, "name": name,
                }

    try:
        lovelace_obj = hass.data.get("lovelace")
        if not lovelace_obj:
            _LOGGER.warning("Lovelace update: no lovelace data in hass.data")
            return

        # Newer HA: LovelaceManager with .dashboards dict
        # Older HA: direct dashboard object or dict
        if hasattr(lovelace_obj, "dashboards"):
            dashboard = lovelace_obj.dashboards.get("lovelace") or lovelace_obj.dashboards.get("")
        elif isinstance(lovelace_obj, dict):
            dashboard = lovelace_obj.get("dashboards", {}).get("lovelace")
        else:
            dashboard = None

        if not dashboard or not hasattr(dashboard, "async_load"):
            _LOGGER.warning("Lovelace update: Overview dashboard not accessible (type=%s)", type(lovelace_obj).__name__)
            return

        config_obj = await dashboard.async_load(force=True)
        if isinstance(config_obj, dict):
            config = config_obj
        elif hasattr(config_obj, "config") and isinstance(getattr(config_obj, "config", None), dict):
            config = config_obj.config
        elif hasattr(config_obj, "data") and isinstance(getattr(config_obj, "data", None), dict):
            config = config_obj.data
        else:
            _LOGGER.warning("Lovelace: cannot extract raw config from %s — skipping", type(config_obj).__name__)
            return
        changed = False

        for view in config.get("views", []):
            path = view.get("path", "")
            if path == "cameras" and cam_info:
                if _patch_cameras_view(view, cam_info):
                    changed = True
            elif path == "ai-detections":
                if _patch_ai_detections_view(view, registry, hass):
                    changed = True

        if any(c.get("area_missing") for c in cam_info.values()):
            if _ensure_area_setup_view(config):
                changed = True

        if changed:
            await dashboard.async_save(config)
            _LOGGER.info("Lovelace views updated (cameras: %d, ai-detections patched)", len(cam_info))
        else:
            _LOGGER.debug("Lovelace update: no changes needed")

    except Exception as err:
        _LOGGER.error("Lovelace update failed: %s", err)


def _normalize_cam_title(title: str) -> str:
    """Normalise a camera title for matching: lowercase, strip accents, strip leading 'camera'."""
    import unicodedata
    s = unicodedata.normalize("NFD", title.lower().strip())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.removeprefix("camera ").strip()


_AREA_SETUP_VIEW_PATH = "tuya-area-setup"

_AREA_SETUP_MARKDOWN = """\
## ⚡ Area not set — motion-alert emails won't be sent for this camera

**Why this matters:** Home Assistant Area is what routes a camera's motion-alert emails to the
right recipients. It is completely separate from how cameras are grouped on the Cameras
dashboard (that's based on the Tuya account/project and doesn't need Area). A camera can look
perfectly normal here and still silently drop every detection email if its Area was never set.

### How to fix it

1. Go to **Settings → Devices & Services → Devices tab**.
2. Search for the camera's name. **Two device entries will match** — this is expected.
3. Open each one and check the **Integration** line at the top. Edit the one that says
   **Tuya** — not **Tuya Cameras**. Setting Area on the Tuya Cameras entry has no effect on
   email routing.
4. Click the pencil/edit icon next to the device name → set **Area** to the correct location →
   **Update**.
5. Back on this Cameras dashboard, press **Refresh All Cameras** (or wait for the next scheduled
   refresh) — the ⚡ warning icon clears automatically once Area is set correctly.

If you're unsure which area name to use, it must match one of the existing "Recipients — area"
entries configured under Tuya Cameras → Configure, otherwise the email will still have nowhere
to go even with an Area set.
"""


def _ensure_area_setup_view(config: dict) -> bool:
    """Create the 'Tuya Area Setup' documentation view if it doesn't exist yet.

    Linked to from the ⚡ warning card injected next to any camera missing an
    Area (see _patch_cameras_view Pass 4). Idempotent — never modifies an
    already-existing view, so any manual edits to it are preserved.
    """
    views = config.setdefault("views", [])
    if any(v.get("path") == _AREA_SETUP_VIEW_PATH for v in views):
        return False
    views.append({
        "title": "Tuya Area Setup",
        "path": _AREA_SETUP_VIEW_PATH,
        "icon": "mdi:lightning-bolt",
        "cards": [{"type": "markdown", "content": _AREA_SETUP_MARKDOWN}],
    })
    _LOGGER.info("Lovelace: created '%s' documentation view", _AREA_SETUP_VIEW_PATH)
    return True


def _patch_cameras_view(view: dict, cam_info: dict) -> bool:
    """
    Sync picture-glance cards in the Cameras view:
    - Update entity IDs for existing cards.
    - Remove cards for cameras no longer in any coordinator.
    - Add new picture-glance cards for cameras that have no card yet,
      placed in the section that already contains cameras from the same
      Tuya SDK project (tuya_cameras config entry). If no such section
      exists, a new section is created. Grouped by project rather than HA
      Area because Area is per-device, manually assigned, and empty for
      any camera not yet touched in Settings — project membership is
      always defined and never requires manual setup.
    """
    norm_map = {_normalize_cam_title(k): v for k, v in cam_info.items()}
    changed  = False

    # ── Pass 1: update / remove existing cards ────────────────────────────
    cameras_with_cards: set[str] = set()   # normalised titles already in the view
    for section in view.get("sections", []):
        cards     = section.get("cards", [])
        new_cards = []
        for card in cards:
            if card.get("type") != "picture-glance":
                new_cards.append(card)
                continue
            title    = card.get("title", "")
            norm     = _normalize_cam_title(title)
            cam      = norm_map.get(norm)
            if cam is None:
                _LOGGER.info("Lovelace: removing card for deleted camera '%s'", title)
                changed = True
                continue
            cameras_with_cards.add(norm)
            new_entities = [e for e in [
                {"entity": cam["sd"]}     if cam["sd"]          else None,
                {"entity": cam["online"]} if cam["online"]       else None,
                {"entity": cam["fmt"]}    if cam["fmt"]          else None,
                {"entity": cam["animal"]} if cam.get("animal")   else None,
            ] if e]
            if new_entities and card.get("entities") != new_entities:
                card["entities"] = new_entities
                _LOGGER.debug("Lovelace: updated entities for card '%s'", title)
                changed = True
            if "camera_view" in card:
                # Self-heal: strip any lingering "live" (or other forced)
                # camera_view on every pass, not just newly-created cards —
                # forces an immediate WebRTC/live stream per card, and go2rtc
                # never retires stale producers (2026-08-10 finding), so
                # simultaneous live cards pile up and starve each other.
                # Default view does lightweight periodic snapshot polling.
                del card["camera_view"]
                _LOGGER.info("Lovelace: removed forced camera_view on card '%s'", title)
                changed = True
            new_cards.append(card)
        if len(new_cards) != len(cards):
            section["cards"] = new_cards

    # ── Pass 2: infer section → project mapping ────────────────────────────
    # Primary: find a picture-glance card whose camera has a known project
    #   (project is always defined — unlike area, never needs a manual match).
    # Fallback: match heading text to a known project name
    #   (handles sections left empty after camera removals).
    section_project: dict[int, str] = {}   # section_index → project name
    known_projects = {v["project"].lower(): v["project"] for v in cam_info.values() if v.get("project")}
    for i, section in enumerate(view.get("sections", [])):
        for card in section.get("cards", []):
            if card.get("type") == "picture-glance":
                norm = _normalize_cam_title(card.get("title", ""))
                cam  = norm_map.get(norm)
                if cam and cam.get("project"):
                    section_project[i] = cam["project"]
                    break
        if i in section_project:
            continue
        # Fallback: match heading text to a known project name (case-insensitive)
        for card in section.get("cards", []):
            if card.get("type") == "heading":
                heading_lc = card.get("heading", "").strip().lower()
                if heading_lc in known_projects:
                    section_project[i] = known_projects[heading_lc]
                    break

    # ── Pass 2.5: relocate cards sitting in an accidental placeholder section ─
    # Self-heals cards added under the old area-grouping bug into a blank,
    # untitled placeholder section instead of the one existing section that
    # already represents their project. Deliberately narrow: only relocates
    # cards out of a section whose heading is blank/missing. A section with
    # any real heading text (e.g. a user manually split "Farm" out of
    # "Winterthur") is never touched — manual dashboard organisation always
    # wins over this heuristic, even when both sections map to the same
    # underlying Tuya project.
    sections = view.get("sections", [])
    canonical_section: dict[str, int] = {}
    for i in sorted(section_project):
        canonical_section.setdefault(section_project[i], i)

    section_heading: dict[int, str] = {}
    for i, section in enumerate(sections):
        heading_card = next((c for c in section.get("cards", []) if c.get("type") == "heading"), None)
        section_heading[i] = (heading_card.get("heading", "") if heading_card else "").strip()

    for i, section in enumerate(sections):
        if section_heading.get(i, ""):
            continue  # real heading — never auto-relocate cards out of it
        cards = section.get("cards", [])
        keep_cards = []
        for card in cards:
            if card.get("type") != "picture-glance":
                keep_cards.append(card)
                continue
            norm = _normalize_cam_title(card.get("title", ""))
            cam  = norm_map.get(norm)
            target_idx = canonical_section.get(cam.get("project")) if cam else None
            if target_idx is not None and target_idx != i:
                sections[target_idx].setdefault("cards", []).append(card)
                _LOGGER.info(
                    "Lovelace: relocated card '%s' from blank section %d to canonical section %d (project=%s)",
                    card.get("title"), i, target_idx, cam.get("project"),
                )
                changed = True
                continue
            keep_cards.append(card)
        if len(keep_cards) != len(cards):
            section["cards"] = keep_cards

    # Drop now-empty placeholder sections (blank heading, no cameras left) —
    # either pre-existing debris or created by this relocation pass.
    kept_sections = []
    for section in sections:
        cards = section.get("cards", [])
        has_camera = any(c.get("type") == "picture-glance" for c in cards)
        only_blank_heading = (
            len(cards) == 1 and cards[0].get("type") == "heading"
            and not cards[0].get("heading", "").strip()
        )
        if not has_camera and only_blank_heading:
            _LOGGER.info("Lovelace: removing empty placeholder section")
            changed = True
            continue
        kept_sections.append(section)
    if len(kept_sections) != len(sections):
        view["sections"] = kept_sections
        # Section indices shifted — rebuild section_project against the new
        # list so Pass 3 below doesn't place cards using stale indices.
        section_project = {}
        for i, section in enumerate(kept_sections):
            for card in section.get("cards", []):
                if card.get("type") == "picture-glance":
                    norm = _normalize_cam_title(card.get("title", ""))
                    cam  = norm_map.get(norm)
                    if cam and cam.get("project"):
                        section_project[i] = cam["project"]
                        break

    # ── Pass 3: add cards for missing cameras ─────────────────────────────
    for cam_key, cam in cam_info.items():
        if _normalize_cam_title(cam_key) in cameras_with_cards:
            continue
        if not cam.get("camera_entity"):
            _LOGGER.debug(
                "Lovelace: skipping new card for '%s' — no camera entity yet (HA restart may be needed)",
                cam.get("name", cam_key),
            )
            continue

        project = cam.get("project", "")
        # Find existing section for this project
        target_idx = next(
            (i for i, p in section_project.items() if p == project),
            None,
        )
        sections = view.setdefault("sections", [])
        if target_idx is None:
            # No section yet groups this project — create one, headed by the
            # project name (only happens the first time a brand-new project's
            # first camera is added; existing projects already have a section
            # inferred via Pass 2 above, so their heading text is untouched).
            sections.append({"cards": [{"type": "heading", "heading": project}]})
            target_idx = len(sections) - 1
            section_project[target_idx] = project

        new_card = {
            "type": "picture-glance",
            "title": cam["name"],
            "camera_image": cam["camera_entity"],
            # No "camera_view": "live" — that forces an immediate WebRTC/live
            # stream per card. go2rtc never retires stale live producers
            # (see 2026-08-10 finding), so simultaneous live cards pile up
            # and starve each other, which is why some snapshots never load.
            # Default view does lightweight periodic snapshot polling instead.
            "entities": [e for e in [
                {"entity": cam["sd"]}     if cam["sd"]          else None,
                {"entity": cam["online"]} if cam["online"]       else None,
                {"entity": cam["fmt"]}    if cam["fmt"]          else None,
                {"entity": cam["animal"]} if cam.get("animal")   else None,
            ] if e],
        }
        sections[target_idx]["cards"].append(new_card)
        _LOGGER.info(
            "Lovelace: added picture-glance card for new camera '%s' (project=%s, entity=%s)",
            cam["name"], project, cam["camera_entity"],
        )
        changed = True

    # ── Pass 4: ⚡ warning card for any camera with no Area set ────────────
    # Area drives motion-alert email routing (unrelated to the project-based
    # grouping above) and is easy to forget on a freshly-added camera — the
    # card links to the "Tuya Area Setup" docs view (_ensure_area_setup_view)
    # so the fix is one tap away instead of buried in project memory.
    _AREA_WARNING_KEY = "_tuya_cameras_area_warning_for"
    for section in view.get("sections", []):
        cards = section.get("cards", [])
        new_cards = []
        i = 0
        while i < len(cards):
            card = cards[i]
            new_cards.append(card)
            i += 1
            if card.get("type") != "picture-glance":
                continue
            norm = _normalize_cam_title(card.get("title", ""))
            cam  = norm_map.get(norm)
            if not cam or not cam.get("dev_id"):
                continue
            # Is the next card already this camera's warning marker?
            existing_warning = (
                i < len(cards) and cards[i].get(_AREA_WARNING_KEY) == cam["dev_id"]
            )
            if cam.get("area_missing"):
                if not existing_warning:
                    new_cards.append({
                        "type": "button",
                        "icon": "mdi:lightning-bolt",
                        "name": f"{cam['name']} — Area not set",
                        "show_state": False,
                        "tap_action": {"action": "navigate", "navigation_path": f"/lovelace/{_AREA_SETUP_VIEW_PATH}"},
                        _AREA_WARNING_KEY: cam["dev_id"],
                    })
                    _LOGGER.info("Lovelace: added Area-missing warning icon for '%s'", cam["name"])
                    changed = True
                else:
                    new_cards.append(cards[i])
                    i += 1
            elif existing_warning:
                # Area was set since the last refresh — drop the stale warning card.
                _LOGGER.info("Lovelace: removed Area-missing warning icon for '%s' (Area now set)", cam["name"])
                i += 1
                changed = True
        if len(new_cards) != len(cards):
            section["cards"] = new_cards

    return changed


def _patch_ai_detections_view(view: dict, registry, hass=None) -> bool:
    """
    Rebuild the AI Detections view sections dynamically:

    • Last Human Detection — one tile per area (deduplicated), using the canonical
      entry for each area (derived from coordinator data when hass is provided).
    • Live Counts — rebuilt from all entries with ai stat sensors.
    • 7-Day Summary — same.

    Sensors are matched by unique_id suffix (_ai_total/_ai_human/_ai_other) so
    entity_id naming differences (e.g. de_ch_br prefix, _2 suffix) are handled
    transparently.
    """
    changed = False
    sections = view.get("sections", [])

    # ── Build area→entry_id and entry_id→label from coordinator data ──────────
    area_canonical: dict[str, str] = {}  # area_slug → entry_id
    entry_label: dict[str, str] = {}     # entry_id → short display label
    if hass:
        for eid, edata in hass.data.get(DOMAIN, {}).items():
            if not isinstance(edata, dict):
                continue
            ce = hass.config_entries.async_get_entry(eid)
            if ce:
                entry_label[eid] = ce.title.replace("Tuya Cameras ", "").strip() or ce.title
            cam_coord = edata.get("coordinator")
            if cam_coord and cam_coord.data:
                for cam in cam_coord.data.get("cameras", {}).values():
                    area = cam.get("area", "")
                    if area:
                        slug = area.lower().replace(" ", "_")
                        if slug not in area_canonical:
                            area_canonical[slug] = eid

    # ── Find ai stat sensors by unique_id suffix (immune to entity_id naming) ─
    entry_stat_eid: dict[str, dict[str, str]] = {}
    for ent in registry.entities.values():
        uid = ent.unique_id or ""
        for suffix, stat in [("_ai_total", "total"), ("_ai_human", "human"), ("_ai_other", "other")]:
            if uid.endswith(suffix):
                entry_stat_eid.setdefault(uid[: -len(suffix)], {})[stat] = ent.entity_id
                break

    def _entry_sort(eid: str) -> tuple:
        return (0 if eid in entry_label else 1, eid)
    sorted_entry_ids = sorted(entry_stat_eid, key=_entry_sort)

    # ── Last Human Detection — one tile per area, canonical entry wins ─────────
    area_candidates: dict[str, list[tuple[str, str]]] = {}
    for ent in registry.entities.values():
        uid = ent.unique_id or ""
        if "_ai_last_human_" not in uid:
            continue
        parts = uid.split("_ai_last_human_", 1)
        if len(parts) != 2:
            continue
        e_id, area_slug = parts
        area_candidates.setdefault(area_slug, []).append((e_id, ent.entity_id))

    last_human: list[tuple[str, str]] = []
    for area_slug, candidates in sorted(area_candidates.items()):
        canonical = area_canonical.get(area_slug)
        # Skip orphaned areas that no longer exist in any coordinator
        if hass and area_canonical and area_slug not in area_canonical:
            continue
        chosen = next((eid for e_id, eid in candidates if e_id == canonical), candidates[0][1])
        area_name = area_slug.replace("_", " ").title()
        last_human.append((area_name, chosen))
    last_human.sort(key=lambda x: x[0])

    new_lh_cards: list[dict] = [{"type": "heading", "heading": "Last Human Detection"}]
    for area_name, sensor_eid in last_human:
        new_lh_cards.append({"type": "tile", "entity": sensor_eid,
                              "name": area_name, "icon": "mdi:account-clock"})

    for section in sections:
        cards = section.get("cards", [])
        if any(c.get("heading") == "Last Human Detection" for c in cards):
            if cards != new_lh_cards:
                section["cards"] = new_lh_cards
                changed = True
            break

    # ── Live Counts and 7-Day Summary — rebuild from actual sensor entities ────
    period = {"calendar": {"period": "week"}}
    for section in sections:
        cards = section.get("cards", [])
        heading = next((c["heading"] for c in cards if c.get("type") == "heading"), None)
        if heading not in ("Live Counts", "7-Day Summary"):
            continue
        is_stat = heading == "7-Day Summary"
        new_cards: list[dict] = [{"type": "heading", "heading": heading}]
        for e_id in sorted_entry_ids:
            s = entry_stat_eid[e_id]
            p_eid = s.get("total")
            h_eid = s.get("human")
            d_eid = s.get("other")
            if not p_eid:
                continue
            short = entry_label.get(e_id, e_id[:8])
            if is_stat:
                new_cards += [
                    {"type": "statistic", "entity": p_eid, "name": f"Processed — {short}",
                     "stat_type": "state", "period": period},
                    {"type": "statistic", "entity": h_eid, "name": f"Human Detected — {short}",
                     "stat_type": "state", "period": period},
                    {"type": "statistic", "entity": d_eid, "name": f"Discarded — {short}",
                     "stat_type": "state", "period": period},
                ]
            else:
                new_cards += [
                    {"type": "tile", "entity": p_eid, "name": f"Processed (7d) — {short}",
                     "icon": "mdi:image-multiple"},
                    {"type": "tile", "entity": h_eid, "name": f"Human Detected (7d) — {short}",
                     "icon": "mdi:account-check", "color": "red"},
                    {"type": "tile", "entity": d_eid, "name": f"Discarded (7d) — {short}",
                     "icon": "mdi:account-off"},
                ]
        if cards != new_cards:
            section["cards"] = new_cards
            changed = True

    if changed:
        _LOGGER.debug("Lovelace: AI Detections view patched (%d areas, %d entries)",
                      len(last_human), len(sorted_entry_ids))
    return changed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    core_entry_id = entry.data[CONF_CORE_ENTRY_ID]

    if DOMAIN_CORE not in hass.data or core_entry_id not in hass.data[DOMAIN_CORE]:
        raise ConfigEntryNotReady("Tuya Home Core is not loaded yet.")

    core        = hass.data[DOMAIN_CORE][core_entry_id]
    tuya_client = core["api"].client
    core_coord  = core["coordinator"]

    smtp_config = {
        CONF_SMTP_HOST:     entry.data[CONF_SMTP_HOST],
        CONF_SMTP_PORT:     entry.data[CONF_SMTP_PORT],
        CONF_SMTP_SENDER:   entry.data[CONF_SMTP_SENDER],
        CONF_SMTP_PASSWORD: entry.data[CONF_SMTP_PASSWORD],
    }
    notifier     = Notifier(smtp_config)
    camera_api   = CameraAPI(tuya_client)
    refresh_days = entry.options.get(CONF_REFRESH_DAYS, DEFAULT_REFRESH_DAYS)
    coordinator  = CameraCoordinator(hass, camera_api, core_coord, refresh_days)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Camera status unavailable: {err}") from err

    # Other integrations (e.g. official Tuya hub) may not have finished loading yet
    # at first-refresh time. Schedule a second refresh after HA startup to pick up
    # runtime_data from those entries (needed for tuya_sharing SD data fallback).
    if not hass.is_running:
        @callback
        def _post_start_refresh(_event=None):
            hass.async_create_task(coordinator.async_refresh())
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _post_start_refresh)
        )

    recipients = entry.options.get(CONF_RECIPIENTS, {})
    uid        = core.get("uid", "")
    access_id  = core["api"]._api_key

    if not uid:
        _LOGGER.warning(
            "Tuya UID not set — MQTT bridge disabled. "
            "Go to Tuya Home Core → Configure and save to trigger auto-detection."
        )

    ai_enabled = entry.options.get(CONF_AI_ENABLED, False)
    ai_url     = entry.options.get(CONF_AI_URL, "")
    ai_client  = AIClient(ai_url) if ai_enabled and ai_url else None
    ai_stats   = AIStats(hass, entry.entry_id)
    await ai_stats.async_load()

    if ai_client:
        _LOGGER.info("AI detection enabled, service: %s", ai_url)

    mqtt_enabled    = entry.options.get(CONF_MQTT_ALERTS_ENABLED, True)
    webhook_enabled = entry.options.get(CONF_WEBHOOK_ALERTS_ENABLED, False)
    animal_cfg      = entry.options.get(CONF_CAMERA_ANIMAL_CONFIG, {})

    bridge = TuyaMQTTBridge(
        hass           = hass,
        tuya_client    = tuya_client,
        camera_api     = camera_api,
        notifier       = notifier,
        recipients_cfg = recipients,
        uid            = uid,
        access_id      = access_id,
        core_coord     = core_coord,
        cam_coord      = coordinator,
        ai_client      = ai_client,
        ai_stats       = ai_stats,
        alerts_enabled = mqtt_enabled,
        entry_label    = entry.title,
        animal_cfg     = animal_cfg,
    )

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {
        "coordinator":            coordinator,
        "core_coord":             core_coord,
        "camera_api":             camera_api,
        "notifier":               notifier,
        "bridge":                 bridge,
        "ai_stats":               ai_stats,
        "webhook_alerts_enabled": webhook_enabled,
        "animal_cfg":             animal_cfg,
    }

    # Start global webhook bridge if any entry has it enabled and it isn't running yet
    if webhook_enabled and "_webhook" not in domain_data:
        wb = SmartLifeWebhookBridge(hass)
        wb.start()
        domain_data["_webhook"] = wb

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _cleanup_stale_entities() -> None:
        """Remove entity registry entries for cameras no longer in coordinator data."""
        current_ids = set((coordinator.data or {}).get("cameras", {}).keys())
        registry = er.async_get(hass)
        for entity in list(registry.entities.values()):
            if entity.config_entry_id != entry.entry_id or entity.platform != DOMAIN:
                continue
            parts = entity.unique_id.split("_")
            if len(parts) < 2 or len(parts[1]) < 18:
                continue
            dev_id = parts[1]
            if dev_id not in current_ids:
                _LOGGER.info("Removing stale entity %s (device %s removed from account)", entity.entity_id, dev_id)
                registry.async_remove(entity.entity_id)

    entry.async_on_unload(
        coordinator.async_add_listener(lambda: hass.async_create_task(_cleanup_stale_entities()))
    )
    await _cleanup_stale_entities()

    bridge.start(hass)
    hass.async_create_task(_update_lovelace_views(hass))
    _LOGGER.info(
        "Tuya Cameras loaded: %d camera(s), refresh every %d day(s)",
        len((coordinator.data or {}).get("cameras", {})),
        refresh_days,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    bridge: TuyaMQTTBridge | None = data.get("bridge")
    if bridge:
        bridge.stop()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Stop webhook bridge if no remaining entry has it enabled
        any_webhook = any(
            v.get("webhook_alerts_enabled", False)
            for v in hass.data[DOMAIN].values()
            if isinstance(v, dict) and "bridge" in v
        )
        if not any_webhook:
            wb: SmartLifeWebhookBridge | None = hass.data[DOMAIN].pop("_webhook", None)
            if wb:
                wb.stop()

    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
