# YOLOv8 Human Detection Extension

This guide extends Tuya Cameras with AI-powered human detection using YOLOv8.
Motion events that contain no human are silently discarded — only confirmed
human presence triggers email alerts.

> **Note:** YOLO runs on the host machine, not inside the HA container.
> It requires ~500 MB RAM and a model file (~6 MB for nano, ~130 MB for medium).

## Prerequisites

```bash
pip install ultralytics pillow
```

Download the nano model (fastest, sufficient for security use):
```bash
wget https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt
```

Place `yolov8n.pt` in `/config/` (same directory as `camera_monitor.py`).

## How it works

The original `camera_monitor.py` (in `homeassistant/config/`) already contains
the full YOLO pipeline. It is intentionally excluded from the HACS integration
because YOLOv8 cannot run inside the HA Docker container and requires host-level
installation.

To enable it, modify `mqtt_bridge.py` in your local installation:

### 1. Add detection function

```python
_yolo_model = None

def _load_yolo(model_path: str):
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO
        _yolo_model = YOLO(model_path)
    return _yolo_model

def has_person(image_bytes: bytes, model_path: str, conf: float = 0.55):
    import io
    from PIL import Image
    model   = _load_yolo(model_path)
    img     = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    results = model.predict(img, verbose=False, classes=[0], conf=conf)
    best    = max((float(b.conf) for r in results for b in r.boxes), default=0.0)
    detected = best >= conf
    try:
        arr = results[0].plot()
        from PIL import Image as PILImage
        buf = io.BytesIO()
        PILImage.fromarray(arr[..., ::-1]).save(buf, format="JPEG", quality=85)
        annotated = buf.getvalue()
    except Exception:
        annotated = image_bytes
    return detected, best, annotated
```

### 2. Call detection in `_handle()` before sending email

In `mqtt_bridge.py`, inside `_handle()`, after `img_bytes` is obtained:

```python
YOLO_MODEL_PATH = "/config/yolov8n.pt"
YOLO_CONF       = 0.55

if img_bytes:
    detected, conf, annotated = await self._hass.async_add_executor_job(
        has_person, img_bytes, YOLO_MODEL_PATH, YOLO_CONF
    )
    if not detected:
        _LOGGER.debug("Motion %s/%s: no human (conf=%.2f) — skipped", area, name, conf)
        return
    img_bytes = annotated
    # (rest of email sending follows)
```

### 3. Confidence tuning

| Value | Effect |
|---|---|
| 0.25 | More sensitive — may false-positive on animals/shadows |
| 0.55 | Recommended — good balance for outdoor cameras |
| 0.75 | Very strict — may miss partially visible people |

### 4. Running outside HA container

If HA runs in Docker, run the MQTT bridge on the host instead:

```bash
python3 /path/to/pulsar_bridge.py
```

And disable the bridge inside the HA integration by setting
`MQTT_BRIDGE_ENABLED = False` in your local `const.py`.

## Performance

| Model | Size | RAM | Inference time (CPU) |
|---|---|---|---|
| yolov8n.pt | 6 MB  | ~200 MB | ~300ms |
| yolov8s.pt | 22 MB | ~400 MB | ~700ms |
| yolov8m.pt | 52 MB | ~800 MB | ~1.5s  |

Nano is sufficient for security camera stills.
