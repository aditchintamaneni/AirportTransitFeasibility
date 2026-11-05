"""Load the Bay Area road network, find shortest driving routes, and drive times to each airport."""

import networkx as nx
import osmnx as ox
import pandas as pd

from src.config import CRS, PROCESSED_DIR, log_step

# road types the public can't drive on, which the osmium filter kept
NOT_DRIVABLE = {"cycleway", "pedestrian", "busway", "construction", "footway", "track", "path", "steps"}
# service roads that aren't through streets; osmnx's own "drive" network drops these too
NOT_PUBLIC_SERVICE = {"parking_aisle", "driveway", "emergency_access", "private"}

# curbside bus stop at each airport's main terminal (from the 511 feed), where a drive trip ends
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
    # built outside the repo: Geofabrik norcal extract (2026-10-27) clipped to the 9 counties and road types with osmium, then ox.graph_from_xml
    # graph is stored in lon/lat, like the graph_from_place graphs from lab 8
    G = ox.io.load_graphml(f"{PROCESSED_DIR}/bay_area_drive.graphml")

    # drop roads the public can't drive: footpaths, parking aisles, driveways, private or no-access roads (e.g. airfield roads)
    before = len(G.edges)
    G.remove_edges_from([(u, v, k) for u, v, k, edge in G.edges(keys=True, data=True) if not public_road(edge)])
    log_step("road_network", "public roads only", before, len(G.edges), "edge is a footpath, parking aisle, driveway, or private/no-access road")

    # keep the largest piece you can drive both into and out of; "strongly" connected respects one-way streets
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
    # snap both points to the nearest road node (X= lon, Y= lat, same as lab 8)
    start = ox.distance.nearest_nodes(G, start_lon, start_lat)
    end, snap_distance = ox.distance.nearest_nodes(G, end_lon, end_lat, return_dist=True)
    print(f"endpoint snapped {snap_distance:.0f} m to the road network")

    # weight="length" minimizes meters driven, not travel time
    route = ox.routing.shortest_path(G, start, end, weight="length")
    if route is None:
        raise ValueError("no route found")
    return ox.routing.route_to_gdf(G, route)[["geometry"]].to_crs(CRS).reset_index(drop=True)


def drive_times(G, centroids, stops):
    """free-flow drive minutes from every tract centroid to each airport's terminal curb."""
    # snap all centroids to their nearest road node at once
    xy = centroids.to_crs(4326).get_coordinates()
    origin_nodes = ox.distance.nearest_nodes(G, xy["x"], xy["y"])

    results = []
    for code, stop_name in TERMINAL_STOPS.items():
        terminal = stops[(stops["code"] == code) & (stops["stop_name"] == stop_name)].to_crs(4326).geometry.iloc[0]
        target = ox.distance.nearest_nodes(G, terminal.x, terminal.y)
        # one search out from the terminal on the reversed graph gives every node's time *to* the terminal (instead of 1,763 separate routes)
        seconds = nx.single_source_dijkstra_path_length(G.reverse(copy=False), target, weight="travel_time")
        results.append(pd.DataFrame({"GEOID": centroids["GEOID"].values, "code": code, "drive_min": [seconds[node] / 60 for node in origin_nodes]}))

    times = pd.concat(results)
    times.to_csv(f"{PROCESSED_DIR}/drive_times.csv", index=False)
    return times
