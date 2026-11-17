-- ================================================================
-- NCT Tracking System — Supabase Schema
-- Run once in: Supabase Dashboard → SQL Editor → New query → Run
-- ================================================================


-- ── 1. Departments ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS departments (
    name        TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO departments (name) VALUES ('ADC') ON CONFLICT DO NOTHING;


-- ── 2. Tracking List ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracking_list (
    nct_id      TEXT  NOT NULL,
    dept        TEXT  NOT NULL REFERENCES departments(name),
    added_date  DATE  DEFAULT CURRENT_DATE,
    added_by    TEXT  DEFAULT 'pipeline',
    PRIMARY KEY (nct_id, dept)
);

CREATE INDEX IF NOT EXISTS idx_tracking_dept ON tracking_list(dept);


-- ── 3. Rejected Trials (blocklist) ───────────────────────────────
CREATE TABLE IF NOT EXISTS rejected_trials (
    nct_id          TEXT  NOT NULL,
    dept            TEXT  NOT NULL REFERENCES departments(name),
    source          TEXT,           -- 'new_candidates' or 'unmatched'
    rejected_date   DATE  DEFAULT CURRENT_DATE,
    PRIMARY KEY (nct_id, dept)
);

CREATE INDEX IF NOT EXISTS idx_rejected_dept ON rejected_trials(dept);


-- ── 4. Organized Trials (upserted each run) ──────────────────────
CREATE TABLE IF NOT EXISTS organized_trials (
    nct_id                  TEXT  PRIMARY KEY,
    dept                    TEXT  NOT NULL REFERENCES departments(name),
    brief_title             TEXT,
    official_title          TEXT,
    overall_status          TEXT,
    phase                   TEXT,
    enrollment              TEXT,
    lead_sponsor            TEXT,
    conditions              TEXT,
    interventions           TEXT,
    start_date              TEXT,
    primary_completion_date TEXT,
    last_update_post_date   TEXT,
    study_first_post_date   TEXT,
    study_url               TEXT,
    full_data               JSONB,  -- all 60+ parsed columns
    last_upserted_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_organized_dept    ON organized_trials(dept);
CREATE INDEX IF NOT EXISTS idx_organized_status  ON organized_trials(overall_status);
CREATE INDEX IF NOT EXISTS idx_organized_sponsor ON organized_trials(lead_sponsor);
CREATE INDEX IF NOT EXISTS idx_organized_phase   ON organized_trials(phase);


-- ── 5. Version Cache (replaces latest_comparison.xlsx) ───────────
CREATE TABLE IF NOT EXISTS version_cache (
    nct_id              TEXT     NOT NULL,
    dept                TEXT     NOT NULL REFERENCES departments(name),
    curr_version        INTEGER,
    curr_date           TEXT,
    curr_status         TEXT,
    note                TEXT,
    total_versions      INTEGER,
    prev_version        INTEGER,
    prev_date           TEXT,
    modules_changed     TEXT,
    field_change_count  INTEGER,
    field_changes       TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (nct_id, dept)
);


-- ── 6. Pipeline State (replaces state.json) ──────────────────────
CREATE TABLE IF NOT EXISTS pipeline_state (
    dept            TEXT  PRIMARY KEY REFERENCES departments(name),
    etag            TEXT,
    last_run_date   DATE,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO pipeline_state (dept) VALUES ('ADC') ON CONFLICT DO NOTHING;


-- ── 7. New Candidates Log ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS new_candidates_log (
    id                  BIGSERIAL PRIMARY KEY,
    nct_id              TEXT  NOT NULL,
    dept                TEXT  NOT NULL REFERENCES departments(name),
    run_date            DATE  NOT NULL,
    decision            TEXT  DEFAULT 'Pending',
    title               TEXT,
    conditions          TEXT,
    interventions       TEXT,
    sponsor             TEXT,
    recruitment_status  TEXT,
    study_first_posted  TEXT,
    last_updated        TEXT,
    link                TEXT,
    decided_at          TIMESTAMPTZ,
    UNIQUE (nct_id, dept, run_date)
);

CREATE INDEX IF NOT EXISTS idx_candidates_decision ON new_candidates_log(dept, decision);
CREATE INDEX IF NOT EXISTS idx_candidates_run_date ON new_candidates_log(run_date);


-- ── 8. Unmatched Log (deduplicated) ─────────────────────────────
CREATE TABLE IF NOT EXISTS unmatched_log (
    nct_id              TEXT  NOT NULL,
    dept                TEXT  NOT NULL REFERENCES departments(name),
    first_seen_date     DATE  NOT NULL,
    last_seen_date      DATE  NOT NULL,
    decision            TEXT  DEFAULT 'Pending',
    title               TEXT,
    conditions          TEXT,
    interventions       TEXT,
    sponsor             TEXT,
    recruitment_status  TEXT,
    link                TEXT,
    decided_at          TIMESTAMPTZ,
    PRIMARY KEY (nct_id, dept)
);

CREATE INDEX IF NOT EXISTS idx_unmatched_decision  ON unmatched_log(dept, decision);
CREATE INDEX IF NOT EXISTS idx_unmatched_last_seen ON unmatched_log(last_seen_date);


-- ── 9. Modified Log ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS modified_log (
    id                  BIGSERIAL PRIMARY KEY,
    nct_id              TEXT  NOT NULL,
    dept                TEXT  NOT NULL REFERENCES departments(name),
    run_date            DATE  NOT NULL,
    last_updated        TEXT,
    recruitment_status  TEXT,
    title               TEXT,
    link                TEXT,
    UNIQUE (nct_id, dept, run_date)
);

CREATE INDEX IF NOT EXISTS idx_modified_run_date ON modified_log(dept, run_date);
CREATE INDEX IF NOT EXISTS idx_modified_nct      ON modified_log(nct_id);


-- ── 10. Field Changes Log (append only) ─────────────────────────
CREATE TABLE IF NOT EXISTS field_changes_log (
    id                  BIGSERIAL PRIMARY KEY,
    nct_id              TEXT  NOT NULL,
    dept                TEXT  NOT NULL REFERENCES departments(name),
    run_date            DATE  NOT NULL,
    note                TEXT,
    total_versions      INTEGER,
    prev_version        INTEGER,
    curr_version        INTEGER,
    prev_date           TEXT,
    curr_date           TEXT,
    curr_status         TEXT,
    modules_changed     TEXT,
    field_change_count  INTEGER,
    field_changes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_field_changes_nct      ON field_changes_log(nct_id, dept);
CREATE INDEX IF NOT EXISTS idx_field_changes_run_date ON field_changes_log(run_date);


-- ── 11. Canonical Changes Log (append only) ──────────────────────
CREATE TABLE IF NOT EXISTS canonical_changes_log (
    id              BIGSERIAL PRIMARY KEY,
    dept            TEXT     NOT NULL REFERENCES departments(name),
    run_date        DATE     NOT NULL,
    requested_nct   TEXT     NOT NULL,
    canonical_nct   TEXT     NOT NULL,
    already_tracked BOOLEAN  DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_canonical_run_date ON canonical_changes_log(dept, run_date);


-- ── 12. Run History ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run_history (
    id              BIGSERIAL PRIMARY KEY,
    dept            TEXT  NOT NULL REFERENCES departments(name),
    run_date        DATE  NOT NULL,
    new_candidates  INTEGER DEFAULT 0,
    unmatched       INTEGER DEFAULT 0,
    modified_trials INTEGER DEFAULT 0,
    field_diffs     INTEGER DEFAULT 0,
    canonical_fixes INTEGER DEFAULT 0,
    duration_s      FLOAT,
    output_file     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_run_history_dept ON run_history(dept, run_date);


-- ── RPC: Unmatched deduplication ────────────────────────────────
-- Preserves team decision on conflict, only updates last_seen_date
CREATE OR REPLACE FUNCTION upsert_unmatched_batch(rows JSONB)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE r JSONB;
BEGIN
    FOR r IN SELECT * FROM jsonb_array_elements(rows) LOOP
        INSERT INTO unmatched_log
            (nct_id, dept, first_seen_date, last_seen_date,
             decision, title, conditions, interventions,
             sponsor, recruitment_status, link)
        VALUES (
            r->>'nct_id', r->>'dept',
            (r->>'run_date')::DATE, (r->>'run_date')::DATE,
            'Pending',
            r->>'title', r->>'conditions', r->>'interventions',
            r->>'sponsor', r->>'recruitment_status', r->>'link'
        )
        ON CONFLICT (nct_id, dept)
        DO UPDATE SET last_seen_date = EXCLUDED.last_seen_date;
        -- decision is intentionally NOT updated — preserves team's choice
    END LOOP;
END;
$$;
