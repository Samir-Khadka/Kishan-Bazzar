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

    conn.commit()
    conn.close()
    print("Seeded: 1 co-op, 5 farmers (1 out of collection radius), 1 staff account, 16 crops.")


if __name__ == "__main__":
    seed()
