"""Transit times from every tract to each airport, using the 511 regional GTFS feed and r5py."""

import datetime
import os
import warnings
import zipfile

import geopandas as gpd
import pandas as pd
import r5py
import requests

from src.config import CRS, FIRST_DEPARTURE_HOUR, LAST_ARRIVAL_HOUR, PROCESSED_DIR, RAW_DIR, SERVICE_DATE, SUNDAY_DATE, log_step

warnings.filterwarnings("ignore", message="The provided departure time window is below 5 minutes")
warnings.filterwarnings("ignore", message="R5 reported the following issues with GTFS file")

GTFS_PATH = f"{RAW_DIR}/511_regional_gtfs.zip"
OSM_PATH = f"{RAW_DIR}/norcal-latest.osm.pbf"
# copy of the feed w the night before's after-midnight trips added (made by add_overnight_trips)
OVERNIGHT_GTFS_PATH = f"{PROCESSED_DIR}/511_gtfs_overnight.zip"

# TIGER's SJC polygon reaches the Santa Clara train station but riders there still need the VTA 60 bus to the terminals
NOT_AIRPORT_STOPS = ["Santa Clara Caltrain Station Northbound", "Santa Clara Station"]

# longest walk counted to or from a stop; r5py's default is the whole trip limit
MAX_WALK = datetime.timedelta(minutes=30)


def download_gtfs():
    """download the 511 regional feed (every Bay Area agency) once; needs a free token from https://511.org/open-data/token"""
    if os.path.exists(GTFS_PATH):
        return
    key = os.environ.get("KEY_511")
    if not key:
        raise ValueError("Set your 511 token first: export KEY_511=your_token")
    response = requests.get(f"https://api.511.org/transit/datafeeds?api_key={key}&operator_id=RG", stream=True)
    if response.status_code != 200:
        raise ValueError(f"511 request failed with status {response.status_code}")
    with open(GTFS_PATH, "wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            file.write(chunk)
    # a wrong token gets a short error message back instead of a zip
    if not zipfile.is_zipfile(GTFS_PATH):
        os.remove(GTFS_PATH)
        raise ValueError("511 sent back an error instead of the feed; check your token")


def read_gtfs(name, path=GTFS_PATH, **kwargs):
    # every column as text so ids keep their leading zeros
    with zipfile.ZipFile(path) as feed, feed.open(name) as file:
        return pd.read_csv(file, dtype=str, **kwargs)


def services_on(day):
    """service_ids running on a date: the weekly calendar, plus added (1) and minus removed (2) dates."""
    calendar, calendar_dates = read_gtfs("calendar.txt"), read_gtfs("calendar_dates.txt")
    date = day.replace("-", "")
    weekday = pd.Timestamp(day).day_name().lower()
    running = (calendar[weekday] == "1") & (calendar["start_date"] <= date) & (calendar["end_date"] >= date)
    exceptions = calendar_dates[calendar_dates["date"] == date]
    added = set(exceptions.loc[exceptions["exception_type"] == "1", "service_id"])
    removed = set(exceptions.loc[exceptions["exception_type"] == "2", "service_id"])
    return (set(calendar.loc[running, "service_id"]) | added) - removed


def airport_stops(airports):
    """boardable transit stops on airport land, using the same TIGER polygons as airport jobs."""
    stops = read_gtfs("stops.txt", usecols=["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"])
    # location_type 0 is a stop you can board at; stations and entrances just group those
    stops = stops[stops["location_type"].fillna("0") == "0"]
    stops = gpd.GeoDataFrame(stops[["stop_id", "stop_name"]], geometry=gpd.points_from_xy(stops["stop_lon"].astype(float), stops["stop_lat"].astype(float)), crs=4326).to_crs(CRS)

    # keep stops inside an airport polygon
    at_airport = gpd.sjoin(stops, airports.to_crs(CRS), predicate="within")
    log_step("gtfs_stops", "inside airport", len(stops), len(at_airport), "stop not on airport land")

    # drop rail platforms the SJC polygon pulls in
    before = len(at_airport)
    at_airport = at_airport[~at_airport["stop_name"].isin(NOT_AIRPORT_STOPS)]
    log_step("gtfs_stops", "drop Santa Clara rail", before, len(at_airport), "rail platform inside the SJC polygon, still a bus ride from the terminals")
    return at_airport[["stop_id", "stop_name", "code", "geometry"]]


def hours(times):
    # "25:30:00" -> 25
    return times.str.split(":").str[0].astype(int)


def add_overnight_trips(days=(SERVICE_DATE, SUNDAY_DATE)):
    """copy the night before's after-midnight trips onto each day, since r5py only runs a day's own trips."""
    trips = read_gtfs("trips.txt")
    # GTFS keeps a 1:30 am trip on the day it started, written as 25:30
    last_night = {day: set(trips.loc[trips["service_id"].isin(services_on(str(pd.Timestamp(day) - pd.Timedelta(days=1))[:10])), "trip_id"]) for day in days}
    wanted = set().union(*last_night.values())

    # stop_times is ~200 MB, so read it in pieces and keep after-midnight stops of last night's trips
    with zipfile.ZipFile(GTFS_PATH) as feed, feed.open("stop_times.txt") as file:
        chunks = pd.read_csv(file, dtype=str, chunksize=1_000_000)
        late = pd.concat(c[c["trip_id"].isin(wanted) & c["departure_time"].notna()] for c in chunks)
    late = late[hours(late["departure_time"]) >= 24]

    new_trips, new_stop_times, new_dates = [], [], []
    for day in days:
        date = day.replace("-", "")
        rows = late[late["trip_id"].isin(last_night[day])].copy()
        copies = trips[trips["trip_id"].isin(rows["trip_id"])].copy()
        log_step("gtfs_trips", f"add overnight trips for {day}", len(trips), len(trips) + len(copies), "trip from the night before still running after midnight")

        # same trip, new id and a service that only runs on this day, times moved back 24 h
        for table in (copies, rows):
            table["trip_id"] = table["trip_id"] + "_overnight_" + date
        copies["service_id"] = "overnight_" + date
        for col in ["arrival_time", "departure_time"]:
            has_time = rows[col].notna()
            rows.loc[has_time, col] = (hours(rows.loc[has_time, col]) - 24).map("{:02d}".format) + rows.loc[has_time, col].str[-6:]
        new_trips.append(copies)
        new_stop_times.append(rows)
        new_dates.append(pd.DataFrame({"service_id": ["overnight_" + date], "date": [date], "exception_type": ["1"]}))

    # write a copy of the feed with the new rows added to the end of each table
    extra = {"trips.txt": pd.concat(new_trips), "stop_times.txt": pd.concat(new_stop_times), "calendar_dates.txt": pd.concat(new_dates)}
    with zipfile.ZipFile(GTFS_PATH) as feed, zipfile.ZipFile(OVERNIGHT_GTFS_PATH, "w") as out:
        for name in feed.namelist():
            data = feed.read(name)
            if name in extra:
                columns = read_gtfs(name, nrows=0).columns
                data = data.rstrip(b"\r\n") + b"\n" + extra[name].reindex(columns=columns).to_csv(header=False, index=False).encode()
            # fixed file dates so every run writes the same bytes and r5py reuses network
            out.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return OVERNIGHT_GTFS_PATH


def transit_network(gtfs_path=OVERNIGHT_GTFS_PATH):
    """r5py's routing network from the OSM streets (for walking to stops) and the GTFS schedules."""
    return r5py.TransportNetwork(OSM_PATH, [gtfs_path], allow_errors=True)


def transit_times(network, centroids, stops, departure, window=datetime.timedelta(minutes=1), max_time=datetime.timedelta(hours=3)):
    """fastest transit + walking minutes from each tract centroid to each airport; with a window, the typical (median) trip over departures in it."""
    origins = centroids.to_crs(4326).rename(columns={"GEOID": "id"})[["id", "geometry"]]
    destinations = stops.to_crs(4326).rename(columns={"stop_id": "id"})[["id", "geometry"]].drop_duplicates("id")
    # travel time includes walking to the stop and waiting there
    times = r5py.TravelTimeMatrix(
        network,
        origins=origins,
        destinations=destinations,
        departure=departure,
        departure_time_window=window,
        transport_modes=[r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK],
        max_time=max_time,
        max_time_walking=MAX_WALK,
        snap_to_network=True,
    )
    # fastest stop at each airport; blank means no trip within max_time
    times = times.merge(stops[["stop_id", "code"]].drop_duplicates("stop_id"), left_on="to_id", right_on="stop_id")
    return times.groupby(["from_id", "code"])["travel_time"].min().reset_index().rename(columns={"from_id": "GEOID", "travel_time": "transit_min"})


def transit_trip_times(network, centroids, stops):
    """typical weekday transit minutes to each airport leaving home 4-5 am (early shift) and 7-8 am (normal commute), to compare with driving."""
    # takes ~20 min, so reuse the saved result; delete the file to recompute
    path = f"{PROCESSED_DIR}/transit_trip_times.csv"
    if os.path.exists(path):
        return pd.read_csv(path, dtype={"GEOID": str})
    results = []
    for hour in [4, 7]:
        start = datetime.datetime.fromisoformat(SERVICE_DATE) + datetime.timedelta(hours=hour)
        times = transit_times(network, centroids, stops, start, window=datetime.timedelta(hours=1))
        times["window"] = f"{hour}-{hour + 1} am"
        results.append(times)
    times = pd.concat(results)
    times.to_csv(path, index=False)
    return times


def earliest_arrival(network, centroids, stops):
    """earliest transit arrival at each airport for every tract, on the weekday and the sunday."""
    path = f"{PROCESSED_DIR}/transit_earliest_arrival.csv"
    if os.path.exists(path):
        first = pd.read_csv(path, dtype={"GEOID": str})
    else:
        results = []
        for day in [SERVICE_DATE, SUNDAY_DATE]:
            # leaving later never gets you there sooner (you could just wait), so one 2 am departure finds the earliest arrival
            # (on 40 sample tracts this matched leaving every 10 min from 2 to 9 am exactly)
            start = datetime.datetime.fromisoformat(day) + datetime.timedelta(hours=FIRST_DEPARTURE_HOUR)
            times = transit_times(network, centroids, stops, start, max_time=datetime.timedelta(hours=LAST_ARRIVAL_HOUR - FIRST_DEPARTURE_HOUR))
            times["day"] = day
            results.append(times)
        first = pd.concat(results)

        # in minutes after midnight (e.g. 5 am = 300); blank means none by 9 am
        first["arrival_min"] = FIRST_DEPARTURE_HOUR * 60 + first.pop("transit_min")
        first["arrival"] = (first["arrival_min"] // 60).map("{:02.0f}".format, na_action="ignore") + ":" + (first["arrival_min"] % 60).map("{:02.0f}".format, na_action="ignore")
        first.to_csv(path, index=False)

    # tracts with no trip stay in as blanks, so they're counted rather than dropped
    log_step("transit", "reaches airport by 9 am", len(first), first["arrival_min"].notna().sum(), "no transit trip leaving at 2 am reaches the airport by 9 am")
    return first
