"""
Collection-route assembly for the co-op route view.

The route view used to be entirely invented sample content (fake
stops, a fake "42 km", a fake vehicle) because no route was ever
computed from data. The data to build an honest one was in the
database all along:

  - pickup stops: farmers who pooled produce into lots that are
    'ready' (formed and priced, awaiting collection), at their
    registered coordinates, with the kilos to collect per farmer;
  - delivery stops: buyers who ordered from those lots, aggregated
    per buyer, at their registered coordinates when they have a
    farmer record;
  - the co-op itself is the start/end point (cooperatives carry
    lat/lon).

Stops are sequenced greedily (nearest unvisited neighbour from the
current point — a reasonable one-vehicle heuristic, not an optimal
TSP solve). Leg distances are haversine straight-line ESTIMATES and
are labelled as such in the payload (`distance_basis`); road
distances would need a routing engine this system doesn't have, and
inventing them was the old view's core dishonesty.

Buyers with no farmer record have no coordinates: they are returned
in `unlocated`, listed with their kg, never placed on the route with
a made-up location.
"""
from aggregation import haversine_km
import repositories


def _nn_order(points, start_lat, start_lon):
    """Greedy nearest-neighbour ordering of points [(lat, lon, row)]
    from a start point. Returns [(row, leg_km_from_previous)]."""
    remaining = list(points)
    ordered = []
    lat, lon = start_lat, start_lon
    while remaining:
        best_i, best_d = 0, None
        for i, (plat, plon, _row) in enumerate(remaining):
            d = haversine_km(lat, lon, plat, plon)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        plat, plon, row = remaining.pop(best_i)
        ordered.append((row, best_d))
        lat, lon = plat, plon
    return ordered, lat, lon


def collection_route(conn, coop_id):
    """The co-op's collection route for its ready lots, built only
    from recorded data. See module docstring for what each part is."""
    coop = repositories.get_cooperative(conn, coop_id)
    if not coop:
        return None

    pickup_rows = repositories.route_pickups(conn, coop_id)
    delivery_rows = repositories.route_deliveries(conn, coop_id)

    pickups = [{
        "kind": "pickup",
        "phone": r["phone"], "name": r["name"],
        "lat": r["lat"], "lon": r["lon"],
        "kg": round(r["kg"], 1),
        "lots": (r["lot_ids"] or "").split(",") if r["lot_ids"] else [],
        "crops": (r["crops"] or "").split(",") if r["crops"] else [],
    } for r in pickup_rows]

    located_deliveries, unlocated_deliveries = [], []
    for r in delivery_rows:
        entry = {
            "kind": "delivery",
            "phone": r["phone"], "name": r["name"],
            "kg": round(r["kg"], 1), "orders": r["orders"],
        }
        if r["lat"] is not None and r["lon"] is not None:
            entry["lat"], entry["lon"] = r["lat"], r["lon"]
            located_deliveries.append(entry)
        else:
            unlocated_deliveries.append(entry)

    # Sequence: co-op -> pickups (nearest-neighbour) -> deliveries
    # (nearest-neighbour continuing from the last pickup) -> co-op.
    stops = []
    total_km = 0.0
    seq = 0
    ordered_pickups, lat, lon = _nn_order(
        [(p["lat"], p["lon"], p) for p in pickups],
        coop["lat"], coop["lon"])
    for row, leg in ordered_pickups:
        seq += 1
        total_km += leg
        stops.append({**row, "seq": seq, "leg_km": round(leg, 1)})
    ordered_deliveries, lat, lon = _nn_order(
        [(d["lat"], d["lon"], d) for d in located_deliveries], lat, lon)
    for row, leg in ordered_deliveries:
        seq += 1
        total_km += leg
        stops.append({**row, "seq": seq, "leg_km": round(leg, 1)})
    return_km = 0.0
    if stops:
        return_km = haversine_km(lat, lon, coop["lat"], coop["lon"])
        total_km += return_km

    return {
        "coop": {"id": coop["id"], "name": coop["name"],
                 "lat": coop["lat"], "lon": coop["lon"]},
        "stops": stops,
        "unlocated": {"deliveries": unlocated_deliveries},
        "totals": {
            "pickup_kg": round(sum(p["kg"] for p in pickups), 1),
            "delivery_kg": round(
                sum(d["kg"] for d in located_deliveries)
                + sum(d["kg"] for d in unlocated_deliveries), 1),
            "stops": len(stops),
            "est_distance_km": round(total_km, 1),
            "return_leg_km": round(return_km, 1),
        },
        # Distances are haversine straight-line estimates between
        # registered coordinates — labelled, never presented as road
        # distance or travel time.
        "distance_basis": "haversine straight-line estimates",
        "source": "live data",
    }
