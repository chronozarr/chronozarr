# Examples

Each directory has a README with its commands. Run the commands from the repository root.

| Directory | What it shows | README |
| --- | --- | --- |
| `anywidget` | A notebook player with a time slider and a play button, controlled from Python. | [README](anywidget/README.md) |
| `bring_your_data` | Your own rasters converted to a store, one pixel read over time, and a static viewer folder. | [README](bring_your_data/README.md) |
| `geemap` | `add_chronozarr` on a geemap MapLibre map, with the NISAR store. | [README](geemap/README.md) |
| `geolibre` | A plugin adapter that adds a chronozarr layer to a GeoLibre map, checked with a mock host. | [README](geolibre/README.md) |
| `leafmap` | `add_chronozarr` on a leafmap MapLibre map, with a date slider. | [README](leafmap/README.md) |
| `nisar` | Two NISAR HH and HV backscatter dates, stored in dB and linear power. | [README](nisar/README.md) |
| `png_frames` | 36 monthly PNG frames with world files, converted to a store and checked bit for bit. | [README](png_frames/README.md) |
| `sentinel2_pc` | Monthly Sentinel-2 median composites from Planetary Computer, encoded as a store. | [README](sentinel2_pc/README.md) |
| `swot_intensity` | Two SWOT intensity dates of Wax Lake, derived from geocoded amplitude. | [README](swot_intensity/README.md) |
| `swot_raster` | Two SWOT water surface elevation dates over the Roanoke region, with quality-filtered variants. | [README](swot_raster/README.md) |
| `water_masks` | NDWI and water-fraction stores derived from the Sentinel-2 mosaics. | [README](water_masks/README.md) |

The `png_frames` and `water_masks` examples read the mosaics that `sentinel2_pc` writes. The `geemap` and `geolibre` examples use the store of `nisar`. The `nisar`, `swot_intensity` and `swot_raster` examples build from source files that are not in this repository.
