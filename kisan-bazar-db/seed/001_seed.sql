-- 001_seed.sql
-- Demo data for the pilot corridor: Kavre/Dhading -> Kathmandu.
-- Coordinates are approximate town centers, fine for a pilot, not survey-grade.

-- ===== wards =====
INSERT INTO wards (district, province, municipality, ward_no, centroid, elevation_band, river_basin) VALUES
('Kavrepalanchok','Bagmati','Dhulikhel',4,  ST_GeogFromText('POINT(85.5442 27.6215)'), 'midhill','bagmati'),
('Kavrepalanchok','Bagmati','Dhulikhel',9,  ST_GeogFromText('POINT(85.5510 27.6180)'), 'midhill','bagmati'),
('Kavrepalanchok','Bagmati','Panauti',2,    ST_GeogFromText('POINT(85.5192 27.5870)'), 'midhill','bagmati'),
('Kavrepalanchok','Bagmati','Banepa',7,     ST_GeogFromText('POINT(85.5220 27.6320)'), 'midhill','bagmati'),
('Kavrepalanchok','Bagmati','Namobuddha',3, ST_GeogFromText('POINT(85.5680 27.5750)'), 'midhill','bagmati'),
('Dhading','Bagmati','Gajuri',5,             ST_GeogFromText('POINT(84.8420 27.8890)'), 'midhill','gandaki'),
('Kathmandu','Bagmati','Kathmandu',11,       ST_GeogFromText('POINT(85.3240 27.7050)'), 'terai','bagmati')
ON CONFLICT DO NOTHING;

-- ===== cooperatives =====
INSERT INTO cooperatives (id, name, registration_no, ward_id, location, phone, collection_radius_m, commission_pct, default_payout_method)
SELECT '11111111-1111-1111-1111-111111111111', 'Kavre Vegetable Co-op', 'COOP-KVR-014',
       id, centroid, '011-490210', 18000, 8.00, 'esewa'
FROM wards WHERE district='Kavrepalanchok' AND municipality='Dhulikhel' AND ward_no=4;

INSERT INTO cooperatives (id, name, registration_no, ward_id, location, phone, collection_radius_m, commission_pct, default_payout_method)
SELECT '22222222-2222-2222-2222-222222222222', 'Dhading Farmer Group', 'COOP-DHD-031',
       id, centroid, '010-560144', 22000, 7.50, 'cash'
FROM wards WHERE district='Dhading' AND municipality='Gajuri' AND ward_no=5;

-- ===== crops (subset matching the prototype) =====
INSERT INTO crops (name_ne, name_en, category, shelf_life_hours, grades) VALUES
('धान','Paddy','cereal', 720, ARRAY['A','B','C']),
('गहुँ','Wheat','cereal', 720, ARRAY['A','B','C']),
('मकै','Maize','cereal', 480, ARRAY['A','B','C']),
('आलु','Potato','vegetable', 240, ARRAY['A','B','C']),
('गोलभेँडा','Tomato','vegetable', 48, ARRAY['A','B','C']),
('काउली','Cauliflower','vegetable', 60, ARRAY['A','B','C']),
('प्याज','Onion','vegetable', 336, ARRAY['A','B','C']),
('तोरी','Mustard','cash_crop', 720, ARRAY['A','B']),
('मुसुरो','Lentil','pulse', 2160, ARRAY['A','B']),
('अदुवा','Ginger','cash_crop', 480, ARRAY['A','B','C']),
('अलैँची','Cardamom','cash_crop', 2160, ARRAY['A','B']),
('जुनार','Junar','fruit', 168, ARRAY['A','B','C']);

-- ===== land types =====
INSERT INTO land_types (code, name_ne, name_en, notes_ne, notes_en) VALUES
('khet','खेत','Irrigated khet','सिँचाइ छ, पानी जम्छ','Irrigated, holds water'),
('bari','बारी','Rain-fed bari','आकाशे पानी मात्र','Rain-fed upland'),
('pakho','पाखो','Sloped pakho','भिरालो, पानी बग्छ','Sloped, drains fast'),
('balaute','बलौटे','Sandy','बालुवा मिसिएको','Sandy, light soil');

-- ===== crop calendar rules (matches the prototype's advisory logic, now sourced) =====
INSERT INTO crop_calendar_rules
    (crop_id, elevation_band, land_type_code, sow_months_bs, duration_days,
     why_ne, why_en, caution_ne, caution_en, source, source_url, reviewed_at)
SELECT c.id, 'midhill', 'khet', ARRAY[5,6,7], 75,
    'नर्सरी अहिले हाल्दा मंसिर-पुसको उच्च भाउ भेटिन्छ।',
    'Nursery now catches the strong Mangsir-Push price.',
    'सहकारीमार्फत मिलाएर बेच्दा भाउ राम्रो पर्छ।',
    'Selling in a pooled co-op lot gets a better rate.',
    'NARC 2081 vegetable crop calendar v3',
    'https://www.narc.gov.np', CURRENT_DATE
FROM crops c WHERE c.name_en = 'Cauliflower';

INSERT INTO crop_calendar_rules
    (crop_id, elevation_band, land_type_code, sow_months_bs, duration_days,
     why_ne, why_en, caution_ne, caution_en, source, source_url, reviewed_at)
SELECT c.id, 'midhill', 'bari', ARRAY[6,7], 100,
    'असोज-कात्तिकमा छर्दा माघमा तेल पेल्न तयार हुन्छ।',
    'Sown now, ready for oil pressing by Magh.',
    'फूल फुल्दा पानी नपरोस् — ओत खोज्ने ठाउँ राख्नुहोस्।',
    'Rain at flowering hurts — plan drainage.',
    'DoA Bagmati Province crop calendar 2080',
    NULL, CURRENT_DATE
FROM crops c WHERE c.name_en = 'Mustard';

-- ===== farmers (Kavre corridor, matches the prototype's co-op pool screen) =====
INSERT INTO users (id, phone, full_name, role, preferred_lang, preferred_channel, ward_id, location, payout_method, payout_handle)
SELECT gen_random_uuid(), '9841000001', 'Sarita Tamang', 'farmer', 'ne', 'app', w.id, w.centroid, 'esewa', '9841000001'
FROM wards w WHERE municipality='Dhulikhel' AND ward_no=4;

INSERT INTO users (id, phone, full_name, role, preferred_lang, preferred_channel, ward_id, location, payout_method, payout_handle)
SELECT gen_random_uuid(), '9841000002', 'Ram Bahadur K.C.', 'farmer', 'ne', 'sms', w.id, w.centroid, 'cash', NULL
FROM wards w WHERE municipality='Panauti' AND ward_no=2;

INSERT INTO users (id, phone, full_name, role, preferred_lang, preferred_channel, ward_id, location, payout_method, payout_handle)
SELECT gen_random_uuid(), '9841000003', 'Mina Shrestha', 'farmer', 'ne', 'app', w.id, w.centroid, 'esewa', '9841000003'
FROM wards w WHERE municipality='Banepa' AND ward_no=7;

INSERT INTO users (id, phone, full_name, role, preferred_lang, preferred_channel, ward_id, location, payout_method, payout_handle)
SELECT gen_random_uuid(), '9841000004', 'Krishna Lama', 'farmer', 'ne', 'app', w.id, w.centroid, 'esewa', '9841000004'
FROM wards w WHERE municipality='Dhulikhel' AND ward_no=9;

-- co-op staff + a buyer, for completeness
INSERT INTO users (id, phone, full_name, role, preferred_lang, preferred_channel)
VALUES (gen_random_uuid(), '9801100000', 'Kavre Co-op Desk', 'coop_staff', 'ne', 'app');

INSERT INTO users (id, phone, full_name, role, preferred_lang, preferred_channel, ward_id)
SELECT gen_random_uuid(), '9812300000', 'Himalayan Hostel Kathmandu', 'buyer', 'en', 'app', w.id
FROM wards w WHERE municipality='Kathmandu' AND ward_no=11;

-- co-op membership
INSERT INTO coop_members (coop_id, farmer_id)
SELECT '11111111-1111-1111-1111-111111111111', id FROM users
WHERE phone IN ('9841000001','9841000002','9841000003','9841000004');

-- ===== listings: enough cauliflower to demonstrate the aggregation threshold =====
INSERT INTO listings (farmer_id, coop_id, crop_id, grade, quantity_kg, location, status, created_via, expires_at)
SELECT u.id, '11111111-1111-1111-1111-111111111111', c.id, 'A', 38,
       u.location, 'open', 'app', now() + interval '60 hours'
FROM users u, crops c WHERE u.phone='9841000001' AND c.name_en='Cauliflower';

INSERT INTO listings (farmer_id, coop_id, crop_id, grade, quantity_kg, location, status, created_via, expires_at)
SELECT u.id, '11111111-1111-1111-1111-111111111111', c.id, 'A', 52,
       u.location, 'open', 'sms', now() + interval '60 hours'
FROM users u, crops c WHERE u.phone='9841000002' AND c.name_en='Cauliflower';

INSERT INTO listings (farmer_id, coop_id, crop_id, grade, quantity_kg, location, status, created_via, expires_at)
SELECT u.id, '11111111-1111-1111-1111-111111111111', c.id, 'B', 21,
       u.location, 'open', 'app', now() + interval '48 hours'
FROM users u, crops c WHERE u.phone='9841000003' AND c.name_en='Tomato';

INSERT INTO listings (farmer_id, coop_id, crop_id, grade, quantity_kg, location, status, created_via, expires_at)
SELECT u.id, '11111111-1111-1111-1111-111111111111', c.id, 'A', 44,
       u.location, 'open', 'app', now() + interval '60 hours'
FROM users u, crops c WHERE u.phone='9841000004' AND c.name_en='Cauliflower';

-- ===== a weather bulletin covering the corridor =====
INSERT INTO weather_bulletins (source, severity, hazard_type, affected_wards, river_basin, message_ne, message_en, valid_from, valid_to)
SELECT 'DHM', 'warning', 'rain',
       array_agg(w.id), 'bagmati',
       'भोलि दिउँसो चट्याङसहित वर्षा — खुला खेतमा नबस्नुहोस्।',
       'Thunderstorms tomorrow afternoon — stay out of open fields.',
       now(), now() + interval '36 hours'
FROM wards w WHERE w.district = 'Kavrepalanchok';
