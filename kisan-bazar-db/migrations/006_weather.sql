-- 006_weather.sql
-- DHM bulletins are authoritative for severity; gridded forecast is supplementary.

CREATE TABLE weather_bulletins (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text NOT NULL,               -- 'DHM' / 'open-meteo' / 'ICIMOD'
    severity        text NOT NULL CHECK (severity IN ('info','advisory','warning','severe')),
    hazard_type     text CHECK (hazard_type IN
                        ('rain','hail','flood','cold_wave','heat_wave','lightning','drought')),
    affected_wards  int[],
    river_basin     text CHECK (river_basin IN
                        ('koshi','bagmati','gandaki','karnali','mahakali',NULL)),
    message_ne      text NOT NULL,
    message_en      text NOT NULL,
    valid_from      timestamptz NOT NULL,
    valid_to        timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_to > valid_from)
);
CREATE INDEX bulletins_active_idx ON weather_bulletins (severity, valid_from, valid_to);

CREATE TABLE alert_deliveries (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bulletin_id   uuid NOT NULL REFERENCES weather_bulletins(id) ON DELETE CASCADE,
    user_id       uuid NOT NULL REFERENCES users(id),
    channel       text NOT NULL CHECK (channel IN ('push','sms','ivr')),
    sent_at       timestamptz,
    status        text NOT NULL DEFAULT 'queued' CHECK (status IN
                        ('queued','sent','failed','skipped')),
    UNIQUE (bulletin_id, user_id, channel)
);
CREATE INDEX alert_deliveries_queue_idx ON alert_deliveries (status)
    WHERE status = 'queued';

-- Finds every user whose ward is covered by a still-valid warning/severe bulletin
-- and who doesn't already have a delivery queued — the core fan-out query.
CREATE OR REPLACE VIEW pending_alert_fanout AS
    SELECT b.id AS bulletin_id, u.id AS user_id,
           CASE WHEN u.preferred_channel = 'app' THEN 'push' ELSE 'sms' END AS channel
    FROM weather_bulletins b
    JOIN users u ON u.ward_id = ANY (b.affected_wards)
    WHERE b.severity IN ('warning','severe')
      AND now() BETWEEN b.valid_from AND b.valid_to
      AND u.is_active
      AND NOT EXISTS (
          SELECT 1 FROM alert_deliveries d
          WHERE d.bulletin_id = b.id AND d.user_id = u.id
      );
