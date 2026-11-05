"""Farmer-scoped routes: land scans and the Crop Doctor. Every route
here is bound to the authenticated farmer (require_auth +
phone-match), so one farmer can never read or write another's
records."""
from flask import Blueprint, g, jsonify, request

import auth
import db
import repositories
from web import require_auth, require_self

bp = Blueprint("farmer", __name__)


VALID_LAND_TYPES = {"khet", "bari", "pakho", "balaute"}
VALID_CONFIDENCE = {"good", "ok"}


@bp.post("/v1/farmers/<phone>/land-scan")
@require_auth
@require_self("phone")
def save_land_scan(phone):
    """Persists a land-scan result from the app's photo+quiz flow. The photo
    itself is never sent here or stored — only the derived signals (colour
    bucket, texture answer, drainage answer) and the resulting land type,
    so this endpoint carries no image data or privacy/storage burden."""
    body = request.get_json(force=True)
    if "land_type" not in body or "confidence" not in body:
        return jsonify({"error": "land_type and confidence are required"}), 400
    if body["land_type"] not in VALID_LAND_TYPES:
        return jsonify({"error": f"land_type must be one of {sorted(VALID_LAND_TYPES)}"}), 400
    if body["confidence"] not in VALID_CONFIDENCE:
        return jsonify({"error": f"confidence must be one of {sorted(VALID_CONFIDENCE)}"}), 400
    # The derived signals are free-form client strings — bound them so
    # an authenticated caller can't bloat the table with unbounded text.
    for field in ("color_key", "texture", "drainage"):
        val = body.get(field)
        if val is not None and len(str(val)) > 64:
            return jsonify({"error": f"{field} too long"}), 400

    conn = db.get_db()
    farmer = repositories.get_farmer(conn, phone)
    if not farmer:
        conn.close()
        return jsonify({"error": "unknown farmer"}), 404

    cur = conn.execute(
        "INSERT INTO land_scans (farmer_phone, land_type, confidence, color_key, texture, drainage, had_photo) "
        "VALUES (?,?,?,?,?,?,?)",
        (phone, body["land_type"], body["confidence"],
         body.get("color_key"), body.get("texture"), body.get("drainage"),
         1 if body.get("had_photo") else 0),
    )
    scan_id = cur.lastrowid
    conn.execute("UPDATE farmers SET land_type=? WHERE phone=?", (body["land_type"], phone))
    conn.commit()
    row = repositories.get_land_scan(conn, scan_id)
    conn.close()
    return jsonify(dict(row)), 201


@bp.get("/v1/farmers/<phone>/land-scan")
@require_auth
@require_self("phone")
def latest_land_scan(phone):
    conn = db.get_db()
    row = repositories.latest_land_scan(conn, phone)
    conn.close()
    if not row:
        return jsonify({"error": "no scan on file for this farmer"}), 404
    return jsonify(dict(row))


@bp.get("/v1/farmers/<phone>/land-scan/history")
@require_auth
@require_self("phone")
def land_scan_history(phone):
    conn = db.get_db()
    rows = repositories.land_scan_history(conn, phone)
    conn.close()
    return jsonify([dict(r) for r in rows])


# Symptom keywords mapped to pest/disease keys in ai_advisor.CROPS_INFO.
# Each entry: (keywords_ne_en, pest_key). Matched against the farmer's
# symptom description to rank likely diagnoses.
SYMPTOM_INDEX = {
    "cauliflower": [
        (["लाही", "aphid", "lahi", "सेतो कीरा", "पात मुनि"], "lahi"),
        (["फेद कुहिने", "damping", "बेर्ना मर्ने", "नर्सरी"], "damping_off"),
        (["गाँठे", "clubroot", "जरा सुन्निने"], "clubroot"),
    ],
    "tomato": [
        (["डढुवा", "blight", "कालो दाग", "पात डढ्ने"], "blight"),
        (["पात खुम्चिने", "curl", "खुम्चिने", "सेतो झिँगा", "whitefly"], "leaf_curl"),
    ],
    "potato": [
        (["खुम्रे", "grub", "khumre", "जमिनमुनि कीरा"], "khumre"),
        (["डढुवा", "blight", "late blight"], "late_blight"),
    ],
    "onion": [
        (["थ्रिप्स", "thrips", "चाँदी", "पात चम्किने"], "thrips"),
    ],
}


@bp.post("/v1/crop-doctor/diagnose")
@require_auth
def crop_doctor_diagnose():
    """Diagnose a crop problem from symptom description (+ optional photo).

    Body: {"crop_code": "GOLB", "symptoms": "पातमा कालो दाग...",
           "farmer_phone": "98...", "photo_ref": "optional-id"}
    Returns ranked diagnoses with remedies from the agronomy KB.
    Photo bytes are NOT sent here — upload separately; photo_ref links them.
    """
    # Rate-limited per IP: every call writes a diagnoses row, so an
    # uncapped caller (one account is trivially obtainable) could bloat
    # the table. 20 per 10 minutes is plenty for interactive use.
    if not auth.check_rate_limit(f"diagnose:{request.remote_addr}", max_attempts=20, window_seconds=600):
        return jsonify({"error": "too many requests, slow down"}), 429
    body = request.get_json(force=True, silent=True) or {}
    crop_code = (body.get("crop_code") or "").strip().upper()
    symptoms = (body.get("symptoms") or "").strip()
    # Free-text fields are stored verbatim — bound them against DB bloat.
    if len(symptoms) > 2000:
        return jsonify({"error": "symptoms too long"}), 400
    photo_ref_in = (body.get("photo_ref") or "").strip()
    if len(photo_ref_in) > 256:
        return jsonify({"error": "photo_ref too long"}), 400
    # The diagnosis is written to the AUTHENTICATED farmer's history.
    # This endpoint used to accept any farmer_phone in the body with
    # no login at all — anyone could poison anyone else's history.
    claimed = (body.get("farmer_phone") or "").strip()
    if claimed and claimed != g.phone:
        return jsonify({"error": "you can only do this for your own phone number"}), 403
    farmer_phone = g.phone
    photo_ref = photo_ref_in or None
    if not crop_code or not symptoms:
        return jsonify({"error": "crop_code and symptoms are required"}), 400

    from ai_advisor import CROPS_INFO
    # Find crop by code
    crop_key = None
    for key, info in CROPS_INFO.items():
        if info.get("code", "").upper() == crop_code or key.upper() == crop_code:
            crop_key = key
            break
    if not crop_key:
        return jsonify({"error": f"unknown crop_code: {crop_code}"}), 400
    info = CROPS_INFO[crop_key]

    # Rank pest/disease entries by symptom keyword matches
    q = symptoms.lower()
    scored = []
    for keywords, pest_key in SYMPTOM_INDEX.get(crop_key, []):
        hits = sum(1 for kw in keywords if kw.lower() in q)
        if hits and pest_key in info.get("pests", {}):
            scored.append((hits, pest_key))
    scored.sort(reverse=True)

    diagnoses = []
    for _, pest_key in scored:
        diagnoses.append({
            "key": pest_key,
            "remedy": info["pests"][pest_key],
        })
    # If nothing matched, return all known issues for the crop
    if not diagnoses:
        diagnoses = [{"key": k, "remedy": v}
                     for k, v in info.get("pests", {}).items()]

    # Save to history
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO diagnoses (farmer_phone, crop_code, symptoms, photo_ref, diagnosis_key, diagnosis_text) "
        "VALUES (?,?,?,?,?,?)",
        (farmer_phone, crop_code, symptoms, photo_ref,
         diagnoses[0]["key"] if diagnoses else None,
         diagnoses[0]["remedy"] if diagnoses else ""))
    conn.commit()
    diag_id = cur.lastrowid
    conn.close()

    return jsonify({
        "diagnosis_id": diag_id,
        "crop": {"ne": info["ne"], "en": info["en"]},
        "diagnoses": diagnoses,
        "note": "यो AI सुझाव हो — निश्चितताका लागि स्थानीय कृषि प्राविधिकसँग परामर्श गर्नुहोस्। "
                "(This is AI guidance — consult your local agri technician to confirm.)",
    }), 200


@bp.get("/v1/crop-doctor/history")
@require_auth
def crop_doctor_history():
    """Past diagnoses for a farmer — the farmer themselves, or staff.
    This used to be public to anyone who knew (or guessed) the phone
    number, free-text symptoms included."""
    phone = request.args.get("farmer_phone", "").strip()
    if not phone:
        return jsonify({"error": "farmer_phone required"}), 400
    conn = db.get_db()
    if phone != g.phone and not repositories.farmer_is_staff(conn, g.phone):
        conn.close()
        return jsonify({"error": "you can only view your own diagnosis history"}), 403
    rows = repositories.diagnoses_for_farmer(conn, phone)
    conn.close()
    return jsonify([dict(r) for r in rows])
