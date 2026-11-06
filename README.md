# Airport Transit Feasibility

Can Bay Area airport workers get to SFO, OAK and SJC by transit in time for early shifts, and what does the alternative cost?

## How to run our analysis

All of the processed data is in `data/processed`, so the figures notebook (`visualizations.ipynb`) runs on its own:

```bash
pip install -r requirements.txt
jupyter notebook visualizations.ipynb   # we ran all our analysis in VSCode
```

To run our entire process end-to-end (LODES airport workers -> airport stops -> transit times w/ r5py -> drive times w/ osmnx -> rideshare fares -> cleaning log):

```bash
export KEY_511=your_token              # get one at https://511.org/open-data/token
jupyter notebook data_processing.ipynb # took about ~20 min, 5 GB RAM locally
```

Before running it, download the Geofabrik extract to `data/raw/norcal-latest.osm.pbf` and run `git lfs pull` to obtain `data/processed/bay_area_drive.graphml`. The notebook downloads the rest.

## Data provenance (raw data)

`data/raw` is git-ignored. Rows marked auto are downloaded by the code on first run; rows not marked auto are obtained manually.

| data | source | saved as |
|---|---|---|
| Jobs by home and work block, 2023 | [LODES 8 CA origin-destination](https://lehd.ces.census.gov/data/lodes/LODES8/ca/od/ca_od_main_JT00_2023.csv.gz) (auto) | `data/raw/ca_lodes_jobs_2023.csv.gz` |
| 2020 census blocks, CA | [TIGER 2020](https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_06_tabblock20.zip) (auto) | `data/raw/ca_blocks_2020.zip` |
| 2020 census tracts, CA (shoreline-clipped, for maps) | [TIGER 2020 cartographic](https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_06_tract_500k.zip) (auto) | `data/raw/ca_tracts_2020_cartographic.zip` |
| Airport polygons | [TIGER 2020 area landmarks](https://www2.census.gov/geo/tiger/TIGER2020/AREALM/tl_2020_06_arealm.zip) (auto, filtered to SFO, OAK, SJC) | `data/raw/ca_landmarks_2020.zip` |
| Transit schedules, every Bay Area agency | [511 regional GTFS](https://511.org/open-data/transit) (auto w/ a free API token) | `data/raw/511_regional_gtfs.zip` |
| Streets, for walking to stops | [Geofabrik Northern California](https://download.geofabrik.de/north-america/us/california/norcal.html) OSM extract (manual) | `data/raw/norcal-latest.osm.pbf` |
| Drive network | the extract above clipped to the 9 counties w/ `osmium extract`, loaded w/ `ox.graph_from_xml` (built once in the terminal) | `data/processed/bay_area_drive.graphml` (git LFS) |

Entered by hand: the Lyft rate cards for Nov 2026 (Silicon Valley from a Lyft driver's app, San Francisco from [getridewise.com](https://getridewise.com/us/california/san-francisco)) and the airport rideshare fees (SFO, OAK, SJC FY 2026/27 schedules) into `src/fares.py`, and the 2026 bridge tolls into `src/network.py`.

## Data description (processed data)

| file | what it holds | made by |
|---|---|---|
| `airport_workers_by_tract.csv` | how many of each airport's 2023 LODES workers live in each home tract | `src/lodes.py`, `src/spatial.py` |
| `tract_centroids.gpkg`, `tract_centroids_4326.gpkg` | one population-weighted center point per tract, in EPSG:32610 and in lon/lat | `src/spatial.py` |
| `transit_earliest_arrival.csv` | for each tract and airport, the earliest transit arrival when leaving home at 2 am, on a Wednesday and a Sunday; blank if nothing arrives by 9 am | `src/transit.py` |
| `transit_trip_times.csv` | for each tract and airport, the median transit trip time for departures between 4 and 5 am and between 7 and 8 am | `src/transit.py` |
| `drive_times.csv` | for each tract and airport, free-flow drive minutes, route miles, the bridge toll paid and which bridges the route crosses | `src/network.py` |
| `transit_vs_drive.csv` | the transit and drive minutes above side by side, for the comparison charts | `data_processing.ipynb` |
| `quoted_fares.csv` | the 120 tracts sampled for Lyft app quotes, w/ the price columns still blank | `src/fares.py` |
| `bay_area_drive.graphml` | the 9-county drive network (git LFS, 830 MB) | locally via CLI |
| `outputs/cleaning_log.csv` | rows before and after every filter in the pipeline, w/ the reason | `src/config.py` |
| `outputs/transit_reach_within_minutes.csv` | share of tracts and workers w/ a transit trip of at most 30, 60 or 90 minutes, by airport and departure window | `visualizations.ipynb` |
| `511_gtfs_overnight.zip` | the 511 feed w/ the night before's after-midnight trips copied onto each day (git-ignored, rebuilt each run) | `src/transit.py` |
