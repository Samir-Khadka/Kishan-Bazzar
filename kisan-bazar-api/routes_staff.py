"""Staff routes: the co-op admin overview and the collection route.
Staff-only — both expose farmer PII (names, locations), so
require_staff guards every route here."""
from flask import Blueprint, g, jsonify

import db
import repositories
import routing
from web import require_auth, require_staff

bp = Blueprint("staff", __name__)


@bp.get("/v1/admin/overview")
@require_auth
@require_staff
def admin_overview():
    """Co-op staff dashboard data. Staff-only: this exposes farmer PII.
    The payload is a read model (repositories.admin_overview) — this
    handler only guards and serializes."""
    conn = db.get_db()
    overview = repositories.admin_overview(conn)
    conn.close()
    return jsonify(overview)


@bp.get("/v1/coop/route")
@require_auth
@require_staff
def coop_route():
    """The collection route for the staff member's co-op, computed
    from live data (routing.collection_route): pickup stops at
    farmers who pooled into ready lots, delivery stops at buyers who
    ordered from them, sequenced nearest-neighbour from the co-op,
    distances as labelled haversine estimates. Staff-only — it
    carries farmer names, phones and coordinates."""
    conn = db.get_db()
    me = repositories.get_farmer(conn, g.phone)
    route = routing.collection_route(conn, me["coop_id"]) if me else None
    conn.close()
    if route is None:
        return jsonify({"error": "co-op not found for this account"}), 404
    return jsonify(route)
