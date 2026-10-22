-- SeatRush schema. Run by the one-shot `migrate` compose service.
--
-- Fully idempotent (CREATE ... IF NOT EXISTS / CREATE OR REPLACE) so
-- `docker compose up` can be run any number of times with no manual steps.
--
-- UUIDs are generated in Python (uuid4) rather than by pgcrypto's
-- gen_random_uuid(), so the schema needs no extensions and the app knows the id
-- before it inserts -- handy for building a response body inside a transaction.

CREATE TABLE IF NOT EXISTS users (
    id            bigserial PRIMARY KEY,
    email         text UNIQUE NOT NULL,
    password_hash text NOT NULL,                 -- hashlib.scrypt, see auth.py
    role          text NOT NULL CHECK (role IN ('user', 'admin')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id             bigserial PRIMARY KEY,
    title          text NOT NULL,
    venue          text NOT NULL,
    starts_at      timestamptz NOT NULL,
    -- The flash sale gate. Bookings before this instant are rejected with 403
    -- sale_not_open, and the check is done with the DATABASE clock (now()) so
    -- a wrong laptop clock can never open a sale early.
    sale_opens_at  timestamptz NOT NULL,
    rows           int NOT NULL CHECK (rows BETWEEN 1 AND 10),
    cols           int NOT NULL CHECK (cols BETWEEN 1 AND 20),
    price_cents    int NOT NULL CHECK (price_cents >= 0),
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- A hold is a short-lived claim on seats: "these are yours for HOLD_SECONDS".
-- Nothing ever deletes an expired hold (there is no background sweeper); a hold
-- is simply *treated* as expired when status='active' AND expires_at < now().
CREATE TABLE IF NOT EXISTS holds (
    id         uuid PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id),
    event_id   bigint NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    status     text NOT NULL CHECK (status IN ('active', 'confirmed', 'released')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seats (
    id       bigserial PRIMARY KEY,
    event_id bigint NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    label    text NOT NULL,                      -- 'C7'
    row_idx  int NOT NULL,                       -- 0-based, 0 => row 'A'
    col_idx  int NOT NULL,                       -- 0-based, 0 => seat 1
    status   text NOT NULL CHECK (status IN ('available', 'held', 'booked')),
    -- Which hold currently owns this seat. The row this points at is what makes
    -- lazy expiry possible: the seat row itself never has to be rewritten when
    -- a hold times out.
    hold_id  uuid REFERENCES holds(id) ON DELETE SET NULL,
    UNIQUE (event_id, label)
);

CREATE TABLE IF NOT EXISTS bookings (
    id           uuid PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users(id),
    event_id     bigint NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    -- One booking per hold, enforced by the database. A duplicate confirm that
    -- somehow slipped past the idempotency layer still cannot create a second
    -- booking from the same hold.
    hold_id      uuid UNIQUE NOT NULL REFERENCES holds(id) ON DELETE CASCADE,
    booking_code text UNIQUE NOT NULL,           -- 'SR-7K2Q9X', shown on the ticket
    total_cents  int NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS booking_seats (
    booking_id uuid   NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    seat_id    bigint NOT NULL REFERENCES seats(id) ON DELETE CASCADE,
    PRIMARY KEY (booking_id, seat_id),
    -- ***THE LAST LINE OF DEFENCE AGAINST OVERSELLING.***
    -- The application logic (the conditional UPDATE in holds.py) is what makes
    -- double-booking impossible in practice. This single-column UNIQUE is what
    -- makes it impossible *even if that logic has a bug*: a seat can appear in
    -- at most one booking, ever, or the INSERT fails and the whole confirm
    -- transaction rolls back. Defence in depth -- correctness guaranteed by the
    -- database, not by the care of whoever edits holds.py next.
    UNIQUE (seat_id)
);

-- Replay store for POSTs. The composite primary key (user_id, key) is the
-- concurrency primitive: two simultaneous inserts of the same key cannot both
-- succeed, so exactly one caller does the work. See idempotency.py.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id       bigint NOT NULL REFERENCES users(id),
    key           text   NOT NULL,
    endpoint      text   NOT NULL,
    request_hash  text   NOT NULL,   -- sha256 of the canonical request body
    status_code   int,
    response_body jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

-- ---------------------------------------------------------------------------
-- Lazy expiry, in one place.
--
-- Every read path (seat map, seats_left, admin stats) reads this view instead
-- of `seats`, so the "a seat held by an expired hold is available" rule is
-- written exactly once and can never drift between endpoints.
--
-- Why lazy expiry is enough for CORRECTNESS: no query anywhere trusts
-- seats.status alone. A seat is claimable when it is available *or* its hold
-- has aged out, and the claim itself is a single conditional UPDATE that
-- re-checks that same condition under a row lock. So an un-swept expired hold
-- can never cause a wrong answer -- only a stale-looking row in the table.
--
-- Why we skipped a background sweeper: it is one more moving part (a worker, a
-- schedule, its own failure modes) that buys no correctness. The trade-off we
-- accept: expired hold rows and stale seats.hold_id pointers linger until the
-- seat is claimed again. At venue scale (<=200 seats/event) that is noise.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW seat_effective AS
SELECT s.id,
       s.event_id,
       s.label,
       s.row_idx,
       s.col_idx,
       s.hold_id,
       CASE
           WHEN s.status = 'held'
                AND h.status = 'active'
                AND h.expires_at < now()
           THEN 'available'
           ELSE s.status
       END AS status
FROM seats s
LEFT JOIN holds h ON h.id = s.hold_id;

-- Indexes chosen from the four hot queries: seat map by event, seat claim by
-- id set, "do I already hold seats for this event", and "my bookings".
CREATE INDEX IF NOT EXISTS seats_event_idx        ON seats (event_id);
CREATE INDEX IF NOT EXISTS seats_hold_idx         ON seats (hold_id);
CREATE INDEX IF NOT EXISTS holds_user_event_idx   ON holds (user_id, event_id, status);
CREATE INDEX IF NOT EXISTS holds_active_idx       ON holds (status, expires_at);
CREATE INDEX IF NOT EXISTS bookings_user_idx      ON bookings (user_id);
CREATE INDEX IF NOT EXISTS bookings_event_idx     ON bookings (event_id);
