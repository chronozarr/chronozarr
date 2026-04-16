"""STAC catalog search for Sentinel-2 L2A on Microsoft Planetary Computer.

Handles:
- STAC search by AOI bbox + date range
- SAS token signing for asset access
- Filtering to required bands (B02/B03/B04/B08 + SCL)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import planetary_computer as pc
import pystac
from pystac_client import Client

logger = logging.getLogger(__name__)

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
S2_COLLECTION = "sentinel-2-l2a"

REQUIRED_BANDS = ("B02", "B03", "B04", "B08")
MASK_BAND = "SCL"
ALL_ASSETS = (*REQUIRED_BANDS, MASK_BAND)


@dataclass(frozen=True)
class SceneRef:
    """Lightweight reference to a single Sentinel-2 L2A scene."""

    item_id: str
    datetime: date
    cloud_cover: float
    epsg: int
    mgrs_tile: str
    asset_hrefs: dict[str, str] = field(repr=False)

    @property
    def band_hrefs(self) -> dict[str, str]:
        return {k: v for k, v in self.asset_hrefs.items() if k in REQUIRED_BANDS}

    @property
    def scl_href(self) -> str:
        return self.asset_hrefs[MASK_BAND]


def search_scenes(
    bbox: tuple[float, float, float, float],
    start: str | date,
    end: str | date,
    max_cloud_pct: float = 80.0,
) -> list[SceneRef]:
    """Search Planetary Computer STAC for S2 L2A scenes.

    Args:
        bbox: (lon_min, lat_min, lon_max, lat_max) in WGS84
        start: Start date (inclusive)
        end: End date (inclusive)
        max_cloud_pct: Maximum cloud cover percentage to include

    Returns:
        List of SceneRef sorted by datetime ascending.
    """
    client = Client.open(PC_STAC_URL, modifier=pc.sign_inplace)

    datetime_str = f"{start}/{end}"
    logger.info(
        "Searching S2 L2A: bbox=%s, datetime=%s, max_cloud=%.0f%%",
        bbox,
        datetime_str,
        max_cloud_pct,
    )

    search = client.search(
        collections=[S2_COLLECTION],
        bbox=bbox,
        datetime=datetime_str,
        query={"eo:cloud_cover": {"lt": max_cloud_pct}},
    )

    scenes: list[SceneRef] = []
    for item in search.items():
        scene = _item_to_scene_ref(item)
        if scene is not None:
            scenes.append(scene)

    scenes.sort(key=lambda s: s.datetime)

    # Deduplicate: same acquisition (date + MGRS tile) can appear multiple times
    # with different processing versions. Keep the latest processing (last in list
    # since PC returns newest processing first, but we sort by datetime).
    before = len(scenes)
    scenes = _deduplicate_scenes(scenes)
    if len(scenes) < before:
        logger.info("Deduplicated %d → %d scenes", before, len(scenes))

    logger.info("Found %d scenes", len(scenes))
    return scenes


def search_scenes_by_month(
    bbox: tuple[float, float, float, float],
    start: str | date,
    end: str | date,
    max_cloud_pct: float = 80.0,
) -> dict[str, list[SceneRef]]:
    """Search and group scenes by YYYY-MM key.

    Returns:
        Dict mapping "YYYY-MM" to list of SceneRef for that month.
    """
    scenes = search_scenes(bbox, start, end, max_cloud_pct)
    by_month: dict[str, list[SceneRef]] = {}
    for scene in scenes:
        key = scene.datetime.isoformat()[:7]  # "YYYY-MM"
        by_month.setdefault(key, []).append(scene)
    logger.info(
        "Grouped into %d months (scene counts: %s)",
        len(by_month),
        {k: len(v) for k, v in by_month.items()},
    )
    return by_month


def _deduplicate_scenes(scenes: list[SceneRef]) -> list[SceneRef]:
    """Keep one scene per (date, mgrs_tile) pair.

    When multiple processing versions exist for the same acquisition,
    keep the one with the longest item_id suffix (typically the latest
    processing timestamp). This avoids downloading the same acquisition twice.
    """
    best: dict[tuple, SceneRef] = {}
    for scene in scenes:
        key = (scene.datetime, scene.mgrs_tile)
        if key not in best or scene.item_id > best[key].item_id:
            best[key] = scene
    return sorted(best.values(), key=lambda s: s.datetime)


def _item_to_scene_ref(item: pystac.Item) -> SceneRef | None:
    """Convert a STAC item to a SceneRef, or None if missing assets."""
    # Check all required assets are present
    missing = [a for a in ALL_ASSETS if a not in item.assets]
    if missing:
        logger.debug("Skipping %s: missing assets %s", item.id, missing)
        return None

    asset_hrefs = {a: item.assets[a].href for a in ALL_ASSETS}

    dt = item.datetime
    if dt is None:
        logger.debug("Skipping %s: no datetime", item.id)
        return None

    cloud_cover = item.properties.get("eo:cloud_cover", 100.0)

    # Planetary Computer uses "proj:code" = "EPSG:32631" rather than "proj:epsg"
    proj_code = item.properties.get("proj:code", "")
    if proj_code.startswith("EPSG:"):
        epsg = int(proj_code.split(":")[1])
    else:
        epsg = item.properties.get("proj:epsg", 0)

    mgrs_tile = item.properties.get("s2:mgrs_tile", "")

    return SceneRef(
        item_id=item.id,
        datetime=dt.date() if hasattr(dt, "date") else dt,
        cloud_cover=cloud_cover,
        epsg=epsg,
        mgrs_tile=mgrs_tile,
        asset_hrefs=asset_hrefs,
    )
