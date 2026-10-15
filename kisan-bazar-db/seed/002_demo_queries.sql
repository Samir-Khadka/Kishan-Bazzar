-- 002_demo_queries.sql
-- Read-only queries that mirror what the prototype's screens would show,
-- run against the seed data. Useful smoke test after migrating + seeding.

-- 1. Co-op "incoming offers" screen: open cauliflower listings, closest first.
SELECT u.full_name, l.quantity_kg, l.grade, w.municipality, w.ward_no
FROM listings l
JOIN users u ON u.id = l.farmer_id
JOIN wards w ON w.id = u.ward_id
JOIN crops c ON c.id = l.crop_id
WHERE c.name_en = 'Cauliflower' AND l.status = 'open'
ORDER BY l.created_at;

-- 2. Aggregation threshold check: has cauliflower crossed a 130kg vehicle-load
--    target for the Kavre co-op? (matches the prototype's "lot ready" banner)
SELECT
    coop_id, crop_id, grade,
    sum(quantity_kg) AS pooled_kg,
    sum(quantity_kg) >= 130 AS threshold_reached
FROM listings
WHERE status = 'open'
  AND coop_id = '11111111-1111-1111-1111-111111111111'
GROUP BY coop_id, crop_id, grade
HAVING sum(quantity_kg) > 0;

-- 3. Advisory screen: what to plant now, for midhill + khet, current BS month.
--    Replace :bs_month with the current month index (0=Baisakh..11=Chaitra).
SELECT c.name_ne, c.name_en, r.why_ne, r.why_en, r.duration_days, r.source
FROM crop_calendar_rules r
JOIN crops c ON c.id = r.crop_id
WHERE r.elevation_band = 'midhill'
  AND r.land_type_code = 'khet'
  AND r.active
  AND 5 = ANY (r.sow_months_bs);   -- 5 = Asoj, matches the prototype's BS_NOW

-- 4. Weather alert fan-out: who should receive the active Kavre rain warning,
--    and on which channel.
SELECT * FROM pending_alert_fanout;

-- 5. Payout SLA check: any farmer payments overdue past 48 hours.
SELECT * FROM payments_sla_breach;
