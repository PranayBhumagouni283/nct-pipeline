-- ─────────────────────────────────────────────────────────────
-- 1. NEW TABLE: dept_indications
--    Stores indication → keyword mappings per dept.
--    Empty string indication = existing asset pipeline.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS "CT".dept_indications (
    id          SERIAL PRIMARY KEY,
    dept        TEXT NOT NULL,
    indication  TEXT NOT NULL,       -- e.g. 'Ovarian Cancer', 'Cervical Cancer'
    keywords    TEXT NOT NULL,       -- comma-separated keyword list
    UNIQUE (dept, indication)
);


GRANT SELECT, INSERT, UPDATE, DELETE
    ON "CT".dept_indications TO pranay;

GRANT USAGE, SELECT
    ON SEQUENCE "CT".dept_indications_id_seq TO pranay;


-- ─────────────────────────────────────────────────────────────
-- 2. tracking_list
--    Current PK: (nct_id, dept)
--    New PK:     (nct_id, dept, indication)
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".tracking_list
    ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT '';

ALTER TABLE "CT".tracking_list
    DROP CONSTRAINT IF EXISTS tracking_list_pkey;

ALTER TABLE "CT".tracking_list
    ADD PRIMARY KEY (nct_id, dept, indication);


-- ─────────────────────────────────────────────────────────────
-- 3. modified_log
--    PK stays on  id  (serial).
--    Current UNIQUE: (nct_id, dept, run_date)
--    New UNIQUE:     (nct_id, dept, indication, run_date)
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".modified_log
    ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT '';

ALTER TABLE "CT".modified_log
    DROP CONSTRAINT IF EXISTS modified_log_nct_id_dept_run_date_key;

ALTER TABLE "CT".modified_log
    ADD UNIQUE (nct_id, dept, indication, run_date);


-- ─────────────────────────────────────────────────────────────
-- 4. field_changes_log
--    PK stays on  id  (serial) — no constraint change needed.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".field_changes_log
    ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT '';


-- ─────────────────────────────────────────────────────────────
-- 5. pipeline_state
--    Current PK: (dept)
--    New PK:     (dept, indication)
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".pipeline_state
    ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT '';

ALTER TABLE "CT".pipeline_state
    DROP CONSTRAINT IF EXISTS pipeline_state_pkey;

ALTER TABLE "CT".pipeline_state
    ADD PRIMARY KEY (dept, indication);


-- ─────────────────────────────────────────────────────────────
-- 6. run_history
--    PK stays on  id  (serial) — no constraint change needed.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".run_history
    ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT '';


-- ─────────────────────────────────────────────────────────────
-- 7. rejected_trials
--    Current PK: (nct_id, dept)
--    New PK:     (nct_id, dept, indication)
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".rejected_trials
    ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT '';

ALTER TABLE "CT".rejected_trials
    DROP CONSTRAINT IF EXISTS rejected_trials_pkey;

ALTER TABLE "CT".rejected_trials
    ADD PRIMARY KEY (nct_id, dept, indication);


-- ─────────────────────────────────────────────────────────────
-- 8. new_candidates_log
--    PK stays on  id  (serial).
--    Current UNIQUE: (nct_id, dept, run_date)
--    New UNIQUE:     (nct_id, dept, indication, run_date)
--    Also adds 5 new scan columns for team review.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".new_candidates_log
    ADD COLUMN IF NOT EXISTS indication      TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS mesh_conditions TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS collaborators   TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS phase           TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS study_type      TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS enrollment      TEXT          DEFAULT '';

ALTER TABLE "CT".new_candidates_log
    DROP CONSTRAINT IF EXISTS new_candidates_log_nct_id_dept_run_date_key;

ALTER TABLE "CT".new_candidates_log
    ADD UNIQUE (nct_id, dept, indication, run_date);


-- ─────────────────────────────────────────────────────────────
-- 9. unmatched_log
--    Current PK: (nct_id, dept)
--    New PK:     (nct_id, dept, indication)
--    Also adds 5 new scan columns for team review.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE "CT".unmatched_log
    ADD COLUMN IF NOT EXISTS indication      TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS mesh_conditions TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS collaborators   TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS phase           TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS study_type      TEXT          DEFAULT '',
    ADD COLUMN IF NOT EXISTS enrollment      TEXT          DEFAULT '';

ALTER TABLE "CT".unmatched_log
    DROP CONSTRAINT IF EXISTS unmatched_log_pkey;

ALTER TABLE "CT".unmatched_log
    ADD PRIMARY KEY (nct_id, dept, indication);



-- ─────────────────────────────────────────────────────────────
-- VERIFICATION — run after migration to confirm all changes
-- ─────────────────────────────────────────────────────────────
SELECT
    tc.table_name,
    tc.constraint_name,
    tc.constraint_type,
    string_agg(kcu.column_name, ', ' ORDER BY kcu.ordinal_position) AS columns
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema    = kcu.table_schema
WHERE tc.table_schema = 'CT'
  AND tc.table_name IN (
      'dept_indications','tracking_list','modified_log','field_changes_log',
      'pipeline_state','run_history','rejected_trials',
      'new_candidates_log','unmatched_log'
  )
GROUP BY tc.table_name, tc.constraint_name, tc.constraint_type
ORDER BY tc.table_name, tc.constraint_type;
