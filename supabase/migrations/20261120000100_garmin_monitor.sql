CREATE TABLE IF NOT EXISTS user_profile (
    user_id text PRIMARY KEY,
    sex text CHECK (sex IN ('male', 'female')),
    birth_date date,
    height_cm integer CHECK (height_cm BETWEEN 100 AND 250),
    target_weight_kg numeric CHECK (target_weight_kg > 0),
    activity_factor numeric CHECK (activity_factor BETWEEN 1 AND 2.5),
    restrictions text[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant_state (
    user_id text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS notification_delivery (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    kind text NOT NULL,
    local_date date NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, kind, local_date)
);

CREATE TABLE IF NOT EXISTS food_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL,
    eaten_at timestamptz NOT NULL,
    source text NOT NULL CHECK (source IN ('photo', 'text')),
    description text NOT NULL,
    analysis jsonb NOT NULL,
    uncertainty text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS food_entries_user_eaten_idx ON food_entries (user_id, eaten_at);

-- Single-user bot: only the server-side database role accesses these tables.
REVOKE ALL ON TABLE user_profile, assistant_state, notification_delivery, food_entries FROM PUBLIC;
REVOKE ALL ON TABLE user_profile, assistant_state, notification_delivery, food_entries FROM anon, authenticated;
