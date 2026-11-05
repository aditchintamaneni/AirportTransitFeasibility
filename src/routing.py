"""Drive distance, free-flow time and bridge tolls from each tract centroid to each airport terminal."""

import os

import geopandas as gpd
import networkx as nx
import osmium
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString

from src.config import CRS, PROCESSED_DIR, log_step

# Geofabrik Northern California extract; the public overpass servers time out on a 9 county query
PBF_URL = "https://download.geofabrik.de/north-america/us/california/norcal-latest.osm.pbf"

# curbside terminal points (lat, lon); the TIGER airport polygons' centroids land on runways
TERMINALS = {
    "SFO": (37.6156, -122.3895),
    "OAK": (37.7126, -122.2125),
    "SJC": (37.3663, -121.9271),
}

# major roads only, so the 9 county graph stays small; centroids snap to the nearest of these
ROADS = ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link"]

# screenlines (lon, lat) drawn across open water mid-span, the direction tolled, and the 2026 2-axle FasTrak toll.
# Carquinez, Benicia-Martinez and Antioch are tolled northbound only, so trips toward the airports never pay them.
BRIDGES = {
    "bay_bridge": (LineString([(-122.377, 37.790), (-122.377, 37.812)]), "west", 8.50),
    "san_mateo": (LineString([(-122.200, 37.560), (-122.200, 37.640)]), "west", 8.50),
    "dumbarton": (LineString([(-122.115, 37.490), (-122.115, 37.520)]), "west", 8.50),
    "richmond_san_rafael": (LineString([(-122.440, 37.915), (-122.440, 37.960)]), "west", 8.50),
    "golden_gate": (LineString([(-122.490, 37.820), (-122.465, 37.820)]), "south", 10.25),
}


def extract_roads(pbf, polygon, path):
    """Write the major-road ways that touch the polygon's bounding box, with their nodes, to an .osm file."""
    west, south, east, north = polygon.bounds
    keep = set()
    ways = osmium.FileProcessor(pbf).with_locations().with_filter(osmium.filter.TagFilter(*[("highway", road) for road in ROADS]))
    for way in ways:
        if way.is_way() and any(n.location.valid() and west < n.lon < east and south < n.lat < north for n in way.nodes):
            keep.add(way.id)
    with osmium.BackReferenceWriter(path, ref_src=pbf, overwrite=True) as writer:
        for way in osmium.FileProcessor(pbf, osmium.osm.WAY).with_filter(osmium.filter.IdFilter(keep)):
            writer.add_way(way)


def load_graph(centroids, pbf):
    """Build (once) the major-road drive network covering every tract centroid, with free-flow travel times.

    pbf is a local copy of PBF_URL; the extracted roads and graph are cached next to it.
    """
    path = pbf.replace(".osm.pbf", "_bay_area_major_roads.graphml")
    if os.path.exists(path):
        return ox.load_graphml(path)

    # 5 km around every centroid, plus the terminals, so rural tracts still reach a highway
    terminals = gpd.GeoSeries(gpd.points_from_xy([lon for _, lon in TERMINALS.values()], [lat for lat, _ in TERMINALS.values()]), crs=4326)
    points = pd.concat([centroids.geometry.to_crs(CRS), terminals.to_crs(CRS)])
    area = points.buffer(5000).union_all().simplify(500)
    polygon = gpd.GeoSeries([area], crs=CRS).to_crs(4326).iloc[0]

    osm = pbf.replace(".osm.pbf", "_bay_area_major_roads.osm")
    if not os.path.exists(osm):
        extract_roads(pbf, polygon, osm)
    G = ox.graph_from_xml(osm, retain_all=False)
    before = len(G)
    G = ox.truncate.truncate_graph_polygon(G, polygon)
    log_step("roads", "inside study area", before, len(G), "road node more than 5 km from every tract centroid")
    # keep only nodes that can reach and be reached from every other node, so every route exists
    before = len(G)
    G = ox.truncate.largest_component(G, strongly=True)
    log_step("roads", "largest strongly connected component", before, len(G), "node cannot reach or be reached from the rest of the network")
    G = ox.add_edge_speeds(G)
    G = ox.add_edge_travel_times(G)
    ox.save_graphml(G, path)
    return G


def toll_edges(G):
    """Map each (u, v, key) edge that crosses a bridge screenline in the tolled direction to its toll."""
    nodes, edges = ox.graph_to_gdfs(G)
    tolls = {}
    for name, (line, direction, toll) in BRIDGES.items():
        crossing = edges[edges.intersects(line)]
        for (u, v, key) in crossing.index:
            if direction == "west":
                tolled = nodes.at[v, "x"] < nodes.at[u, "x"]
            else:
                tolled = nodes.at[v, "y"] < nodes.at[u, "y"]
            if tolled:
                tolls[(u, v, key)] = (name, toll)
    found = {name for name, _ in tolls.values()}
    missing = set(BRIDGES) - found
    if missing:
        print(f"warning: no tolled edges found for {sorted(missing)}; check the screenlines")
    return tolls


def tract_airport_routes(G, centroids):
    """Fastest free-flow route from every tract centroid to every terminal: miles, minutes, tolls."""
    centroids = centroids.to_crs(4326)
    # snap only to nodes off freeways, since a centroid snapped onto a one-way bridge approach gets a phantom toll
    freeway = {"motorway", "motorway_link", "trunk", "trunk_link"}
    # simplified edges that merge several road types store highway as a list
    surface = [(u, v) for u, v, hw in G.edges(data="highway") if not set(hw if isinstance(hw, list) else [hw]) <= freeway]
    pickup = {n for edge in surface for n in edge}
    tract_nodes = ox.distance.nearest_nodes(G.subgraph(pickup), centroids.geometry.x, centroids.geometry.y)
    tolls = toll_edges(G)
    reverse = G.reverse(copy=False)

    rows = []
    for code, (lat, lon) in TERMINALS.items():
        target = ox.distance.nearest_nodes(G, lon, lat)
        # one dijkstra out from the terminal on the reversed graph gives every node's next hop toward the terminal
        next_hop, _ = nx.dijkstra_predecessor_and_distance(reverse, target, weight="travel_time")

        for geoid, node in zip(centroids["GEOID"], tract_nodes):
            meters = seconds = toll = 0.0
            bridges = []
            while node != target:
                nxt = next_hop[node][0]
                key, data = min(G[node][nxt].items(), key=lambda kv: kv[1]["travel_time"])
                meters += data["length"]
                seconds += data["travel_time"]
                if (node, nxt, key) in tolls:
                    name, amount = tolls[(node, nxt, key)]
                    bridges.append(name)
                    toll += amount
                node = nxt
            rows.append({"code": code, "GEOID": geoid, "miles": meters / 1609.344, "minutes": seconds / 60, "toll": toll, "bridges": ";".join(bridges)})

    routes = pd.DataFrame(rows)
    routes.to_csv(f"{PROCESSED_DIR}/tract_airport_routes.csv", index=False)
    return routes
