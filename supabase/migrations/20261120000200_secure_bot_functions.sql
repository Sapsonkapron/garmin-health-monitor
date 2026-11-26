ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS age_years integer CHECK (age_years BETWEEN 18 AND 120);

CREATE OR REPLACE FUNCTION merge_assistant_state(p_key text, p_changes jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    result jsonb;
BEGIN
    IF p_key NOT IN ('assistant', 'alerts', 'weekly') OR jsonb_typeof(p_changes) <> 'object' THEN
        RAISE EXCEPTION 'invalid state update';
    END IF;
    INSERT INTO assistant_state (user_id, key, value)
    VALUES ('serhii', p_key, '{}')
    ON CONFLICT (user_id, key) DO NOTHING;
    UPDATE assistant_state
    SET value = COALESCE((
        SELECT jsonb_object_agg(entry.key, entry.value)
        FROM jsonb_each(assistant_state.value || p_changes) AS entry
        WHERE entry.value <> 'null'::jsonb
    ), '{}'), updated_at = now()
    WHERE user_id = 'serhii' AND key = p_key
    RETURNING value INTO result;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION confirm_food_entry(
    p_id uuid, p_eaten_at timestamptz, p_source text, p_description text,
    p_analysis jsonb, p_uncertainty text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    result jsonb;
BEGIN
    IF p_source NOT IN ('photo', 'text') OR length(p_description) > 2000 OR length(p_uncertainty) > 1000 THEN
        RAISE EXCEPTION 'invalid food entry';
    END IF;
    INSERT INTO food_entries (id, user_id, eaten_at, source, description, analysis, uncertainty)
    VALUES (p_id, 'serhii', p_eaten_at, p_source, p_description, p_analysis, p_uncertainty)
    RETURNING to_jsonb(food_entries.*) INTO result;
    UPDATE assistant_state SET value = value - 'pending_food', updated_at = now()
    WHERE user_id = 'serhii' AND key = 'assistant' AND value->'pending_food'->>'id' = p_id::text;
    RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION merge_assistant_state(text, jsonb) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION confirm_food_entry(uuid, timestamptz, text, text, jsonb, text) FROM PUBLIC, anon, authenticated;

INSERT INTO user_profile (user_id, sex, age_years, height_cm, target_weight_kg, activity_factor)
VALUES ('serhii', 'male', 37, 179, 95, 1.3)
ON CONFLICT (user_id) DO NOTHING;
