"""examples/nisar/ingest.py: scene selection, the window on the grid and the dB encoding."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest


def load_ingest():
    """The example script as a module, by path: another example is also called `ingest`."""
    path = Path(__file__).resolve().parents[1] / "examples" / "nisar" / "ingest.py"
    spec = importlib.util.spec_from_file_location("nisar_ingest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module's annotations here
    spec.loader.exec_module(module)
    return module


ingest = load_ingest()

pytestmark = pytest.mark.unit

COLLECTION = "NISAR_L2_GCOV_PROVISIONAL_V1"


def granule(
    cycle: int,
    track: int = 5,
    direction: str = "A",
    frame: int = 18,
    counter: int = 1,
    flags: str = "N_F_J",
) -> str:
    day = 10 + cycle
    return (
        f"NISAR_L2_PR_GCOV_{cycle:03d}_{track:03d}_{direction}_{frame:03d}_4005_DHDH_A_"
        f"202601{day:02d}T125110_202601{day:02d}T125145_P05023_{flags}_{counter:03d}"
    )


def scene(cycle: int, **kw) -> ingest.Scene:
    name = granule(cycle, **kw)
    day = 10 + cycle
    parsed = ingest.parse_scene(
        {
            "sceneName": name,
            "startTime": f"2026-01-{day:02d}T12:51:10Z",
            "url": f"https://x/{name}.h5",
        },
        COLLECTION,
    )
    assert parsed is not None
    return parsed


def test_parse_scene_reads_the_name_fields():
    s = scene(3, track=7, direction="D", frame=42, counter=2, flags="N_P_J")
    assert (s.cycle, s.track, s.direction, s.frame, s.mode, s.pol, s.counter) == (
        3,
        7,
        "D",
        42,
        "4005",
        "DHDH",
        2,
    )
    assert s.full_frame is False
    assert scene(3).full_frame is True
    assert ingest.parse_scene({"sceneName": "NISAR_L2_UR_GCOV_whatever"}, COLLECTION) is None


def test_select_scenes_takes_the_series_with_most_dates_and_the_highest_counter():
    scenes = [
        scene(1),
        scene(2),
        scene(3),
        scene(3, counter=2),
        scene(1, track=9),
        scene(2, frame=19),
    ]
    key, selected = ingest.select_scenes(scenes)
    assert key == (5, "A", 18)
    assert [s.cycle for s in selected] == [1, 2, 3]
    assert selected[-1].counter == 2
    key, pinned = ingest.select_scenes(scenes, frame=19)
    assert key == (5, "A", 19) and [s.cycle for s in pinned] == [2]
    with pytest.raises(SystemExit):
        ingest.select_scenes(scenes, track=99)


def test_stored_db_quantizes_to_a_hundredth_of_a_db_and_clips_at_minus_60():
    power = np.array([[1.0, 0.1, 1e-7, 1e-9, 0.0, np.nan]], dtype=np.float32)
    valid = np.array([[True, True, True, True, False, False]])
    stored = ingest.stored_db(power, valid)
    assert stored.dtype == np.uint16
    db = stored.astype(np.float64) * ingest.DB_SCALE + ingest.DB_OFFSET
    assert db[0, :2] == pytest.approx([0.0, -10.0], abs=0.005)
    assert db[0, 2] == pytest.approx(-60.0, abs=0.005)  # clipped: -70 dB is below the floor
    assert db[0, 3] == pytest.approx(-60.0)  # -90 dB, clipped
    assert stored[0, 4] == 0 and stored[0, 5] == 0  # invalid pixels store 0


def test_validity_needs_a_focused_subswath_and_positive_finite_power():
    mask = np.array([1, 2, 0, 255, 1, 1, 1], dtype=np.uint8)
    hh = np.array([0.5, 0.5, 0.5, 0.5, 0.0, np.nan, 0.5], dtype=np.float32)
    hv = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1, np.inf], dtype=np.float32)
    assert ingest.validity(mask, hh, hv).tolist() == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]


def test_window_on_and_slices_of_follow_the_grid_exactly():
    x = 1000.0 + 10.0 * np.arange(100) + 5.0  # pixel centres
    y = 5000.0 - 10.0 * np.arange(80) - 5.0
    window = ingest.window_on(x, y, 32612, (1203.0, 4402.0, 1497.0, 4701.0))
    assert (window.width, window.height) == (30, 30)
    assert window.x == (1205.0, 1495.0) and window.y == (4695.0, 4405.0)
    assert window.transform == [10.0, 0.0, 1200.0, 0.0, -10.0, 4700.0]
    rows, cols = ingest.slices_of(window, x, y, 32612, "g")
    assert (rows, cols) == (slice(30, 60), slice(20, 50))
    # the same window on a scene whose grid starts elsewhere but is congruent
    rows2, cols2 = ingest.slices_of(window, x[10:], y[5:], 32612, "g")
    assert (rows2, cols2) == (slice(25, 55), slice(10, 40))
    with pytest.raises(SystemExit, match="same grid"):
        ingest.slices_of(window, x + 3.0, y, 32612, "g")
    with pytest.raises(SystemExit, match="EPSG:32611"):
        ingest.slices_of(window, x, y, 32611, "g")
    with pytest.raises(SystemExit, match="does not overlap"):
        ingest.window_on(x, y, 32612, (9000.0, 9000.0, 9100.0, 9100.0))


def test_utc_naive_strips_the_zone_and_refuses_a_naive_stamp():
    assert ingest.utc_naive("2026-01-11T12:51:10Z") == ingest.datetime(2026, 1, 11, 12, 51, 10)
    assert ingest.utc_naive("2026-01-11T14:51:10+02:00") == ingest.datetime(
        2026, 1, 11, 12, 51, 10
    )
    with pytest.raises(ValueError):
        ingest.utc_naive("2026-01-11T12:51:10")
