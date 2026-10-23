"""Staff routes: the co-op admin overview. Staff-only — it exposes
farmer PII, so require_staff guards every route here."""
import os
import re

from flask import Blueprint, request, jsonify, g, current_app

import db
import aggregation
import auth
import assistant
import repositories
import availability
from web import (
    DEMO_MODE, DEFAULT_TARGET_KG, MAX_REASONABLE_KG,
    require_auth, require_self, require_staff, idempotent,
    valid_positive_kg, reject,
)

bp = Blueprint("staff", __name__)


@bp.get("/v1/admin/overview")
@require_auth
@require_staff
def admin_overview():
    """Co-op staff dashboard data. Staff-only: this exposes farmer PII."""
    conn = db.get_db()
    farmers = conn.execute("SELECT phone, name, coop_id, lat, lon, payout_method, land_type, is_staff FROM farmers").fetchall()
    listings = conn.execute("SELECT id, farmer_phone, coop_id, crop_code, grade, quantity_kg, quantity_committed_kg, status, created_at FROM listings ORDER BY id DESC LIMIT 50").fetchall()
    # Row payloads capped: lots/orders grow per transaction, and the admin
    # dashboard only renders the 5 most recent lots. Totals come from
    # COUNT(*)/SUM(*) so the KPI cards stay exact past the cap.
    lots = conn.execute("SELECT id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status, created_at FROM lots ORDER BY created_at DESC LIMIT 200").fetchall()
    crops = conn.execute("SELECT code, name_ne, name_en, shelf_life_hours FROM crops").fetchall()
    orders = conn.execute("SELECT id, buyer_name, lot_id, quantity_kg, unit_rate, created_at FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    total_lots = conn.execute("SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    total_orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    total_listings = conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
    # total_kg must aggregate the whole table: the old code summed only the
    # 50 fetched listings, so the KPI silently undercounted past 50 listings.
    total_kg = conn.execute("SELECT COALESCE(SUM(quantity_kg), 0) AS s FROM listings").fetchone()["s"]
    pooled_kg = conn.execute("SELECT COALESCE(SUM(pooled_kg), 0) AS s FROM lots").fetchone()["s"]
    conn.close()

    return jsonify({
        "farmers": [dict(r) for r in farmers],
        "listings": [dict(r) for r in listings],
        "lots": [dict(r) for r in lots],
        "crops": [dict(r) for r in crops],
        "orders": [dict(r) for r in orders],
        "counts": {
            "farmers": len(farmers),
            "listings": total_listings,
            "lots": total_lots,
            "orders": total_orders,
            "total_kg": round(total_kg, 1),
            "pooled_kg": round(pooled_kg, 1)
        }
    })
