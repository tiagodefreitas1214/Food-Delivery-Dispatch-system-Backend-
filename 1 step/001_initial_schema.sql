-- =============================================================================
-- 001_initial_schema.sql
-- Dispatch system — Phase 1 schema
-- Platform: PostgreSQL 14+
-- Run: psql -d dispatch_db -f migrations/001_initial_schema.sql
-- =============================================================================

-- Required for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- ENUM TYPES
-- =============================================================================

CREATE TYPE driver_status AS ENUM (
    'online_available',  -- Ready to receive an assignment
    'online_busy',       -- Currently carrying an order
    'offline'            -- Not working / app closed
);

CREATE TYPE dispatch_status AS ENUM (
    'pending',           -- Created; prep time being calculated
    'holding',           -- predictedReadyAt > 10 min away; no driver needed yet
    'assigned',          -- Driver locked and confirmed
    'picked_up',         -- Driver has collected the order from restaurant
    'delivered',         -- Order delivered to customer
    'cancelled'          -- Cancelled at any stage
);

-- =============================================================================
-- DRIVERS
-- Represents a delivery driver in the field.
--
-- Key invariants enforced by the dispatch loop:
--   - Only drivers with status = 'online_available' are candidates.
--   - Drivers whose last_location_at is stale (> 5 min) are excluded,
--     even if their status says online_available.
--   - current_order_id is set atomically alongside status = 'online_busy'
--     inside a SELECT FOR UPDATE lock.
-- =============================================================================

CREATE TABLE drivers (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(100)    NOT NULL,
    phone               VARCHAR(20)     NOT NULL UNIQUE,

    -- Operational state
    status              driver_status   NOT NULL DEFAULT 'offline',
    current_lat         DOUBLE PRECISION,           -- NULL when offline
    current_lng         DOUBLE PRECISION,           -- NULL when offline
    last_location_at    TIMESTAMPTZ,                -- Heartbeat; used for staleness check

    -- Set atomically by the dispatch loop (FK added after orders table)
    current_order_id    UUID,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- ORDERS
-- Represents a customer order from creation through delivery.
--
-- Fields that drive dispatch logic:
--   - predicted_ready_at   → computed at order creation (Step 3)
--   - dispatch_status      → state machine driven by dispatch loop (Step 4)
--   - assigned_driver_id   → set atomically under row lock (Step 5)
--
-- restaurant_id / customer_id are UUIDs referencing tables out of Phase 1 scope.
-- Their coordinates are stored denormalised here to avoid joins in the hot path.
-- =============================================================================

CREATE TABLE orders (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Restaurant (coordinates denormalised for dispatch hot-path)
    restaurant_id       UUID            NOT NULL,
    restaurant_lat      DOUBLE PRECISION NOT NULL,
    restaurant_lng      DOUBLE PRECISION NOT NULL,

    -- Customer / delivery destination
    customer_id         UUID            NOT NULL,
    delivery_lat        DOUBLE PRECISION NOT NULL,
    delivery_lng        DOUBLE PRECISION NOT NULL,

    -- Dispatch state machine
    dispatch_status     dispatch_status  NOT NULL DEFAULT 'pending',
    predicted_ready_at  TIMESTAMPTZ      NOT NULL,

    -- Assignment (written atomically by dispatch loop)
    assigned_driver_id  UUID            REFERENCES drivers(id) ON DELETE SET NULL,
    assigned_at         TIMESTAMPTZ,    -- When the lock was committed

    -- Lifecycle timestamps (for SLA tracking)
    picked_up_at        TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- CROSS-REFERENCE FK  (drivers.current_order_id → orders.id)
-- Added after orders table exists to resolve the circular dependency.
-- ON DELETE SET NULL: if an order is hard-deleted, driver becomes free.
-- =============================================================================

ALTER TABLE drivers
    ADD CONSTRAINT fk_drivers_current_order_id
    FOREIGN KEY (current_order_id)
    REFERENCES orders(id)
    ON DELETE SET NULL;

-- =============================================================================
-- INDEXES
-- Every index below serves a specific query in the dispatch loop.
-- Do not add speculative indexes before profiling.
-- =============================================================================

-- Dispatch loop: "Find all orders that are ready to evaluate"
-- Query: WHERE dispatch_status IN ('pending', 'holding')
CREATE INDEX idx_orders_dispatch_status
    ON orders(dispatch_status);

-- Dispatch loop: "Is predictedReadyAt within the 10-min window?"
-- Query: WHERE predicted_ready_at <= NOW() + INTERVAL '10 minutes'
CREATE INDEX idx_orders_predicted_ready_at
    ON orders(predicted_ready_at);

-- Lookup: which order is assigned to a given driver?
CREATE INDEX idx_orders_assigned_driver_id
    ON orders(assigned_driver_id);

-- Driver filtering: "Give me all online_available drivers"
CREATE INDEX idx_drivers_status
    ON drivers(status);

-- Staleness check: "Exclude drivers silent for > 5 minutes"
-- Query: WHERE last_location_at >= NOW() - INTERVAL '5 minutes'
CREATE INDEX idx_drivers_last_location_at
    ON drivers(last_location_at);

-- =============================================================================
-- UPDATED_AT TRIGGER
-- Automatically maintains updated_at on every row mutation.
-- Avoids relying on application-layer timestamp management.
-- =============================================================================

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_drivers_updated_at
    BEFORE UPDATE ON drivers
    FOR EACH ROW
    EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION fn_set_updated_at();

-- =============================================================================
-- END OF MIGRATION
-- =============================================================================
