"""Load the Bay Area road network and find shortest driving routes."""

import networkx as nx
import osmnx as ox

from src.config import CRS, PROCESSED_DIR, log_step


def load_road_network():
    """load the 9 county drive graph (built from the Geofabrik OSM extract) and keep its main connected piece."""
    # built outside the repo: Geofabrik norcal extract (2026-10-27) clipped to the 9 counties and road types with osmium, then ox.graph_from_xml
    # graph is stored in lon/lat, like the graph_from_place graphs from lab 8
    G = ox.io.load_graphml(f"{PROCESSED_DIR}/bay_area_drive.graphml")

    # keep the largest connected piece so routes can't start or end on an isolated road
    components = list(nx.weakly_connected_components(G))
    largest = max(components, key=len)
    G_main = G.subgraph(largest)
    log_step("road_network", "largest connected component", len(G.nodes), len(G_main.nodes), f"node in one of {len(components) - 1} small pieces not connected to the main network")
    return G_main


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
