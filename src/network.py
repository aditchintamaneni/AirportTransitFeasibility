"""Load the Bay Area road network, find shortest driving routes, and drive times to each airport."""

import networkx as nx
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString

from src.config import CRS, PROCESSED_DIR, log_step

# bridge screenlines (lon, lat) drawn across open water mid span, the tolled direction, and the 2026 2-axle FasTrak toll 
# Carquinez, Benicia-Martinez and Antioch are tolled northbound only, so trips toward the airports never pay them
BRIDGES = {
    "bay_bridge": (LineString([(-122.377, 37.790), (-122.377, 37.812)]), "west", 8.50),
    "san_mateo": (LineString([(-122.200, 37.560), (-122.200, 37.640)]), "west", 8.50),
    "dumbarton": (LineString([(-122.115, 37.490), (-122.115, 37.520)]), "west", 8.50),
    "richmond_san_rafael": (LineString([(-122.440, 37.915), (-122.440, 37.960)]), "west", 8.50),
    "golden_gate": (LineString([(-122.490, 37.820), (-122.465, 37.820)]), "south", 10.25),
}
FREEWAY = {"motorway", "motorway_link", "trunk", "trunk_link"}

# road types people cannot drive on
NOT_DRIVABLE = {"cycleway", "pedestrian", "busway", "construction", "footway", "track", "path", "steps"}
# service roads that aren't through streets
NOT_PUBLIC_SERVICE = {"parking_aisle", "driveway", "emergency_access", "private"}

# curbside bus stop at each airport's main terminal (from the 511 feed, where a drive trip ends)
TERMINAL_STOPS = {
    "SFO": "Terminal 2 Lower Level Bus Stop",
    "OAK": "Oakland International Airport",
    "SJC": "SJC Airport Terminal B",
}


def as_set(value):
    # an osm tag can be one value, a list, or missing
    if value is None:
        return set()
    return set(value) if isinstance(value, list) else {value}


def public_road(edge):
    highway, service, access = as_set(edge.get("highway")), as_set(edge.get("service")), as_set(edge.get("access"))
    if not highway or highway <= NOT_DRIVABLE or access & {"private", "no"}:
        return False
    return not ("service" in highway and service & NOT_PUBLIC_SERVICE)


def load_road_network():
    """load the 9 county drive graph, keep public roads we can route on, and add travel times."""
    # built by osmium extract cmd (cut norcal-lastest-osm.pbf to 9-county polygon, keep ways tagged highway)
    # graph is stored in lon/lat, same as graph_from_place graphs from lab 8
    G = ox.io.load_graphml(f"{PROCESSED_DIR}/bay_area_drive.graphml")

    before = len(G.edges)
    G.remove_edges_from([(u, v, k) for u, v, k, edge in G.edges(keys=True, data=True) if not public_road(edge)])
    log_step("road_network", "public roads only", before, len(G.edges), "edge is a footpath, parking aisle, driveway, or private/no-access road")

    # keep only strongly connected components, or largest piece one can drive both ways in
    components = list(nx.strongly_connected_components(G))
    largest = max(components, key=len)
    before = len(G.nodes)
    G.remove_nodes_from([node for node in G.nodes if node not in largest])
    log_step("road_network", "largest connected component", before, len(G.nodes), f"node in one of {len(components) - 1} small pieces you can't drive both into and out of")

    # speeds from maxspeed tags, filled in by road type where missing, then seconds per edge (lab 8)
    G = ox.add_edge_speeds(G)
    G = ox.add_edge_travel_times(G)
    return G


def shortest_route(G, start_lon, start_lat, end_lon, end_lat):
    """shortest driving route by distance between two lon/lat points, as edges in our CRS."""
    start = ox.distance.nearest_nodes(G, start_lon, start_lat)
    end, snap_distance = ox.distance.nearest_nodes(G, end_lon, end_lat, return_dist=True)
    print(f"endpoint snapped {snap_distance:.0f} m to the road network")

    # weight="length" minimizes meters driven, not time
    route = ox.routing.shortest_path(G, start, end, weight="length")
    if route is None:
        raise ValueError("no route found")
    return ox.routing.route_to_gdf(G, route)[["geometry"]].to_crs(CRS).reset_index(drop=True)


def toll_edges(G):
    """edges that cross a bridge screenline in the tolled direction -> (bridge, toll)"""
    x, y = nx.get_node_attributes(G, "x"), nx.get_node_attributes(G, "y")
    tolls = {}
    for name, (line, direction, toll) in BRIDGES.items():
        west, south, east, north = line.bounds
        for u, v, key, data in G.edges(keys=True, data=True):
            # cheap box check before the geometry test
            if min(x[u], x[v]) > east or max(x[u], x[v]) < west or min(y[u], y[v]) > north or max(y[u], y[v]) < south:
                continue
            if data.get("geometry", LineString([(x[u], y[u]), (x[v], y[v])])).intersects(line):
                tolled = x[v] < x[u] if direction == "west" else y[v] < y[u]
                if tolled:
                    tolls[(u, v, key)] = (name, toll)
    return tolls


def drive_times(G, centroids, stops):
    """free-flow drive minutes, miles and bridge tolls from every tract centroid to each airport's terminal curb."""
    # snap centroids to nodes off freeways, so one dropped onto a bridge approach doesn't get a phantom toll (Matt T)
    pickup = {n for u, v, hw in G.edges(data="highway") if not as_set(hw) <= FREEWAY for n in (u, v)}
    xy = centroids.to_crs(4326).get_coordinates()
    origin_nodes = ox.distance.nearest_nodes(G.subgraph(pickup), xy["x"], xy["y"])
    tolls = toll_edges(G)
    reverse = G.reverse(copy=False)

    rows = []
    for code, stop_name in TERMINAL_STOPS.items():
        terminal = stops[(stops["code"] == code) & (stops["stop_name"] == stop_name)].to_crs(4326).geometry.iloc[0]
        target = ox.distance.nearest_nodes(G, terminal.x, terminal.y)
        # one search out from the terminal on the reversed graph gives every node's time to the terminal and its next hop toward it
        next_hop, seconds = nx.dijkstra_predecessor_and_distance(reverse, target, weight="travel_time")
        for geoid, origin in zip(centroids["GEOID"], origin_nodes):
            # walk the route to add up miles and tolls
            node, meters, toll, bridges = origin, 0.0, 0.0, []
            while node != target:
                nxt = next_hop[node][0]
                key, data = min(G[node][nxt].items(), key=lambda kv: kv[1]["travel_time"])
                meters += data["length"]
                if (node, nxt, key) in tolls:
                    bridges.append(tolls[(node, nxt, key)][0])
                    toll += tolls[(node, nxt, key)][1]
                node = nxt
            rows.append({"GEOID": geoid, "code": code, "drive_min": seconds[origin] / 60, "miles": meters / 1609.344, "toll": toll, "bridges": ";".join(bridges)})

    times = pd.DataFrame(rows)
    times.to_csv(f"{PROCESSED_DIR}/drive_times.csv", index=False)
    return times
