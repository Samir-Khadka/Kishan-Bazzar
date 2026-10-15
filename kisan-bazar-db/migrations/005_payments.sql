-- 005_payments.sql
-- Farmer payouts. Deliberately decoupled from buyer payment collection —
-- the platform/co-op carries collection risk, not the farmer.

CREATE TABLE payments (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id              text REFERENCES lots(id),
    farmer_id           uuid NOT NULL REFERENCES users(id),
    listing_id          uuid REFERENCES listings(id),
    gross_amount        numeric(10,2) NOT NULL CHECK (gross_amount >= 0),
    coop_commission     numeric(10,2) NOT NULL CHECK (coop_commission >= 0),
    net_amount          numeric(10,2) NOT NULL CHECK (net_amount >= 0),
    method              text NOT NULL CHECK (method IN ('esewa','khalti','fonepay','cash')),
    provider_txn_id     text,
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN
                            ('pending','paid','failed')),
    failure_reason      text,
    retry_count         int NOT NULL DEFAULT 0,
    initiated_at        timestamptz NOT NULL DEFAULT now(),
    settled_at          timestamptz,
    CHECK (net_amount = gross_amount - coop_commission)
);
CREATE INDEX payments_farmer_idx ON payments (farmer_id, initiated_at DESC);
CREATE INDEX payments_pending_idx ON payments (status, initiated_at)
    WHERE status = 'pending';

-- Surfaces any payout that has blown the 48-hour SLA promised to farmers.
CREATE OR REPLACE VIEW payments_sla_breach AS
    SELECT * FROM payments
    WHERE status = 'pending'
      AND initiated_at < now() - interval '48 hours';
