"""OpenSlide-backed deep-zoom tiling for whole-slide images (.svs).

Slides live in object storage; on first access the file is pulled to a local cache
and opened with OpenSlide, then DeepZoom tiles are served to an OpenSeadragon viewer.
OpenSlide + its native library come from the `openslide-bin` wheel — no apt needed.
"""

import os
import threading
from io import BytesIO

import storage

CACHE_DIR = os.environ.get("SLIDE_CACHE_DIR", "/tmp/slide-cache")
TILE_SIZE = 254
OVERLAP = 1

os.makedirs(CACHE_DIR, exist_ok=True)

# asset_id -> (OpenSlide, DeepZoomGenerator); guarded by _registry_lock for open,
# and a per-slide lock for reads (OpenSlide region reads are serialized to be safe).
_slides: dict = {}
_locks: dict = {}
_registry_lock = threading.Lock()


def _open(asset_id: int, uri: str):
    """Return (OpenSlide, DeepZoomGenerator, Lock), opening + caching on first use."""
    # Imported lazily so the rest of the API still starts even if OpenSlide is missing.
    import openslide
    from openslide.deepzoom import DeepZoomGenerator

    with _registry_lock:
        if asset_id not in _slides:
            ext = os.path.splitext(uri)[1] or ".svs"
            path = os.path.join(CACHE_DIR, f"{asset_id}{ext}")
            if not os.path.exists(path):
                storage.download_file(uri, path)
            osr = openslide.OpenSlide(path)
            dz = DeepZoomGenerator(osr, tile_size=TILE_SIZE, overlap=OVERLAP, limit_bounds=True)
            _slides[asset_id] = (osr, dz)
            _locks[asset_id] = threading.Lock()
    return (*_slides[asset_id], _locks[asset_id])


def info(asset_id: int, uri: str) -> dict:
    """Viewer metadata: full dimensions, tile geometry, level count, microns-per-pixel."""
    import openslide

    osr, dz, _ = _open(asset_id, uri)
    width, height = dz.level_dimensions[-1]
    props = osr.properties

    def _num(key):
        try:
            return float(props.get(key))
        except (TypeError, ValueError):
            return None

    return {
        "asset_id": asset_id,
        "width": width,
        "height": height,
        "tile_size": TILE_SIZE,
        "overlap": OVERLAP,
        "levels": dz.level_count,
        "mpp_x": _num(openslide.PROPERTY_NAME_MPP_X),
        "mpp_y": _num(openslide.PROPERTY_NAME_MPP_Y),
        "objective_power": _num(openslide.PROPERTY_NAME_OBJECTIVE_POWER),
    }


def tile(asset_id: int, uri: str, level: int, col: int, row: int) -> bytes:
    """Return one DeepZoom tile as JPEG bytes."""
    _osr, dz, lock = _open(asset_id, uri)
    if level < 0 or level >= dz.level_count:
        raise IndexError("level out of range")
    with lock:
        img = dz.get_tile(level, (col, row))
    buf = BytesIO()
    img.convert("RGB").save(buf, "jpeg", quality=80)
    return buf.getvalue()


def thumbnail(asset_id: int, uri: str, size: int = 800) -> bytes:
    """Return a downscaled overview of the whole slide as JPEG bytes."""
    osr, _dz, lock = _open(asset_id, uri)
    with lock:
        img = osr.get_thumbnail((size, size))
    buf = BytesIO()
    img.convert("RGB").save(buf, "jpeg", quality=85)
    return buf.getvalue()
