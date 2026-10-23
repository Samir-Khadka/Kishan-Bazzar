"""Seed data for manual testing / demoing the API — same Kavre corridor
farmers used in the SQL seed and the SMS/USSD demo, so results are
comparable across all three implementations."""
import db


def seed():
    db.init_db(fresh=True)
    conn = db.get_db()

    conn.execute(
        "INSERT INTO cooperatives (id, name, lat, lon, collection_radius_km, commission_pct) "
        "VALUES ('kavre-veg','Kavre Vegetable Co-op', 27.6215, 85.5442, 18, 8.0)"
    )

    farmers = [
        ("9841000001", "Sarita Tamang", "kavre-veg", 27.6215, 85.5442),   # Dhulikhel-4, at co-op
        ("9841000002", "Ram Bahadur K.C.", "kavre-veg", 27.5870, 85.5192), # Panauti-2, ~7km away
        ("9841000003", "Mina Shrestha", "kavre-veg", 27.6320, 85.5220),   # Banepa-7, ~6km away
        ("9841000004", "Krishna Lama", "kavre-veg", 27.6180, 85.5510),    # Dhulikhel-9, ~1km away
        ("9841000005", "Bishnu Ghimire", "kavre-veg", 27.9500, 85.9000),  # deliberately far — ~50km, outside radius
    ]
    conn.executemany(
        "INSERT INTO farmers (phone, name, coop_id, lat, lon) VALUES (?,?,?,?,?)", farmers
    )

    # co-op staff — same phone used as "Kavre Co-op Desk" in the SMS/USSD
    # project, kept consistent across artifacts. is_staff=1 is what lets
    # this phone (and only this phone) confirm/price lots via the API.
    conn.execute(
        "INSERT INTO farmers (phone, name, coop_id, lat, lon, is_staff) VALUES (?,?,?,?,?,1)",
        ("9801100000", "Kavre Co-op Desk", "kavre-veg", 27.6215, 85.5442),
    )

    # Same 16 crops and the same short codes the app (CROP_API_CODE) and the
    # SMS/USSD system use — one shared vocabulary across all three, so a
    # listing made in any channel is valid in the aggregation engine.
    # shelf_life_hours matches the Postgres seed where a value exists there.
    crops = [
        ("KAULI", "काउली", "Cauliflower", 60),
        ("GOLB", "गोलभेँडा", "Tomato", 48),
        ("ALU", "आलु", "Potato", 240),
        ("PYAJ", "प्याज", "Onion", 336),
        ("TORI", "तोरी", "Mustard", 720),
        ("MUSURO", "मुसुरो", "Lentil", 2160),
        ("ADUWA", "अदुवा", "Ginger", 480),
        ("ALAICHI", "अलैँची", "Cardamom", 2160),
        ("JUNAR", "जुनार", "Junar", 168),
        ("SYAU", "स्याउ", "Apple", 240),
        ("FAPAR", "फापर", "Buckwheat", 720),
        ("KERAU", "केराउ", "Pea", 72),
        ("MULA", "मुला", "Radish", 96),
        ("DHAN", "धान", "Paddy", 720),
        ("GAHU", "गहुँ", "Wheat", 720),
        ("MAKAI", "मकै", "Maize", 480),
    ]
    conn.executemany(
        "INSERT INTO crops (code, name_ne, name_en, shelf_life_hours) VALUES (?,?,?,?)", crops
    )

    # Demo machinery rentals — hill-appropriate equipment
    rentals = [
        ("9841000002", "Ram Bahadur K.C.", "power_tiller", "Sifang Power Tiller 12HP", 1500, "Dhulikhel", "9841000002"),
        ("9841000003", "Mina Shrestha", "mini_tiller", "Mini Tiller 7HP", 800, "Banepa", "9841000003"),
        ("9841000004", "Krishna Lama", "sprayer", "Battery Sprayer 20L", 300, "Panauti", "9841000004"),
        ("9841000001", "Sarita Tamang", "thresher", "Paddy Thresher", 1200, "Dhulikhel", "9841000001"),
    ]
    conn.executemany(
        "INSERT INTO rentals (owner_phone, owner_name, machine_type, machine_name, rate_per_day, location, contact_phone) "
        "VALUES (?,?,?,?,?,?,?)", rentals
    )

    # Demo lots — ready for buyers, matching the frontend demo data shape
    conn.executemany(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES (?,?,?,?,?,?,?,?)", DEMO_LOTS
    )

    conn.commit()
    conn.close()
    print("Seeded: 1 co-op, 5 farmers (1 out of collection radius), 1 staff account, 16 crops, 4 rentals, 5 lots.")


# The demo lots seed() inserts — also used by app.py's startup
# migration for databases seeded before lots existed (2026-10-16).
DEMO_LOTS = [
    ("KVR-118", "kavre-veg", "KAULI", "A", 500, 410, 58, "ready"),
    ("DHD-207", "kavre-veg", "GOLB", "A", 350, 300, 48, "ready"),
    ("ILM-044", "kavre-veg", "ALAICHI", "A", 30, 25, 1450, "ready"),
    ("MST-012", "kavre-veg", "SYAU", "A", 200, 150, 138, "ready"),
    ("SND-091", "kavre-veg", "JUNAR", "B", 250, 200, 95, "ready"),
]


def ensure_demo_lots(conn):
    """Backfill the demo lots if the lots table is empty (and the
    kavre-veg co-op exists to satisfy the FK). Idempotent."""
    lot_count = conn.execute("SELECT COUNT(*) AS n FROM lots").fetchone()["n"]
    if lot_count:
        return
    coop_exists = conn.execute(
        "SELECT 1 FROM cooperatives WHERE id='kavre-veg'").fetchone()
    if not coop_exists:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES (?,?,?,?,?,?,?,?)", DEMO_LOTS)
    conn.commit()


if __name__ == "__main__":
    seed()
