-- ================================================================
-- NCT Tracking System — Complete Database Schema


-- ── 1. Departments ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS departments (
    name        TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO departments (name) VALUES ('ADC')  ON CONFLICT DO NOTHING;
INSERT INTO departments (name) VALUES ('ASMB') ON CONFLICT DO NOTHING;


-- ── 2. Tracking List ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracking_list (
    nct_id      TEXT  NOT NULL,
    dept        TEXT  NOT NULL REFERENCES departments(name),
    added_date  DATE  DEFAULT CURRENT_DATE,
    added_by    TEXT  DEFAULT 'pipeline',
    PRIMARY KEY (nct_id, dept)
);

CREATE INDEX IF NOT EXISTS idx_tracking_dept ON tracking_list(dept);


-- ── 3. Rejected Trials──────────────────────────────
CREATE TABLE IF NOT EXISTS rejected_trials (
    nct_id        TEXT  NOT NULL,
    dept          TEXT  NOT NULL REFERENCES departments(name),
    source        TEXT,
    rejected_date DATE  DEFAULT CURRENT_DATE,
    PRIMARY KEY (nct_id, dept)
);

CREATE INDEX IF NOT EXISTS idx_rejected_dept ON rejected_trials(dept);


-- ── 4. Organized Trials ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS organized_trials (
    nct_id                             TEXT  PRIMARY KEY,
    dept                               TEXT  NOT NULL REFERENCES departments(name),
    "Study URL"                        TEXT  DEFAULT '',
    "Other IDs"                        TEXT  DEFAULT '',
    "Brief Title"                      TEXT  DEFAULT '',
    "Official Title"                   TEXT  DEFAULT '',
    "Acronym"                          TEXT  DEFAULT '',
    "Org Full Name"                    TEXT  DEFAULT '',
    "Overall Status"                   TEXT  DEFAULT '',
    "Status Verified Date"             TEXT  DEFAULT '',
    "Exclusion Rationale"              TEXT  DEFAULT '',
    "Expanded Access Info"             TEXT  DEFAULT '',
    "Start Date"                       TEXT  DEFAULT '',
    "Start DateType"                   TEXT  DEFAULT '',
    "Primary Completion Date"          TEXT  DEFAULT '',
    "Primary Completion DateType"      TEXT  DEFAULT '',
    "completionDateStructDate"         TEXT  DEFAULT '',
    "completionDateStructType"         TEXT  DEFAULT '',
    "studyFirstSubmitDate"             TEXT  DEFAULT '',
    "studyFirstSubmitQcDate"           TEXT  DEFAULT '',
    "Study First Post Date"            TEXT  DEFAULT '',
    "studyFirstPostDateType"           TEXT  DEFAULT '',
    "Last Update Submit Date"          TEXT  DEFAULT '',
    "Last Update Post Date"            TEXT  DEFAULT '',
    "lastUpdatePostDateType"           TEXT  DEFAULT '',
    "Sponsors"                         TEXT  DEFAULT '',
    "Collaborators"                    TEXT  DEFAULT '',
    "Funder Type"                      TEXT  DEFAULT '',
    "responsiblePartyType"             TEXT  DEFAULT '',
    "responsiblePartyleadSponsor"      TEXT  DEFAULT '',
    "responsiblePartyleadSponsorclass" TEXT  DEFAULT '',
    "FDA Regulated Drug"               TEXT  DEFAULT '',
    "FDA Regulated Device"             TEXT  DEFAULT '',
    "Has DMC"                          TEXT  DEFAULT '',
    "briefSummary"                     TEXT  DEFAULT '',
    "detailedDescription"              TEXT  DEFAULT '',
    "conditions"                       TEXT  DEFAULT '',
    "studyType"                        TEXT  DEFAULT '',
    "phases"                           TEXT  DEFAULT '',
    "Allocation"                       TEXT  DEFAULT '',
    "Intervention Model"               TEXT  DEFAULT '',
    "Masking"                          TEXT  DEFAULT '',
    "Primary Purpose"                  TEXT  DEFAULT '',
    "Enrollment"                       TEXT  DEFAULT '',
    "enrollmentInfoType"               TEXT  DEFAULT '',
    "Interventions"                    TEXT  DEFAULT '',
    "Primary Outcomes"                 TEXT  DEFAULT '',
    "Secondary Outcomes"               TEXT  DEFAULT '',
    "eligibilityCriteria"              TEXT  DEFAULT '',
    "Sex"                              TEXT  DEFAULT '',
    "Minimum Age"                      TEXT  DEFAULT '',
    "Standard Ages"                    TEXT  DEFAULT '',
    "healthyVolunteers"                TEXT  DEFAULT '',
    "Central Contacts"                 TEXT  DEFAULT '',
    "Overall Officials"                TEXT  DEFAULT '',
    "Locations"                        TEXT  DEFAULT '',
    "Study Documents"                  TEXT  DEFAULT '',
    "Reference Count"                  TEXT  DEFAULT '',
    "PMIDs"                            TEXT  DEFAULT '',
    "Citations"                        TEXT  DEFAULT '',
    "IPD Sharing"                      TEXT  DEFAULT '',
    "MeSH Conditions"                  TEXT  DEFAULT '',
    "MeSH Interventions"               TEXT  DEFAULT '',
    "Primary Drug"                     TEXT  DEFAULT '',
    "_parsed_date"                     TEXT  DEFAULT '',
    last_upserted_at                   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_organized_dept    ON organized_trials(dept);
CREATE INDEX IF NOT EXISTS idx_organized_status  ON organized_trials("Overall Status");
CREATE INDEX IF NOT EXISTS idx_organized_phase   ON organized_trials("phases");
CREATE INDEX IF NOT EXISTS idx_organized_sponsor ON organized_trials("Sponsors");


-- ── 5. Version Cache──────────
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


-- ── 6. Pipeline State─────────────────────
CREATE TABLE IF NOT EXISTS pipeline_state (
    dept          TEXT  PRIMARY KEY REFERENCES departments(name),
    etag          TEXT,
    last_run_date DATE,
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO pipeline_state (dept) VALUES ('ADC')  ON CONFLICT DO NOTHING;
INSERT INTO pipeline_state (dept) VALUES ('ASMB') ON CONFLICT DO NOTHING;


-- ── 7. New Candidates Log ───────────────────────────────────────
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


-- ── 8. Unmatched Log────────────────────────────
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


-- ── 9. Modified Log ─────────────────────────────────────────────
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


-- ── 10. Field Changes Log────────────────────────
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
    field_changes       TEXT,
    curr_full_data      JSONB
);

CREATE INDEX IF NOT EXISTS idx_field_changes_nct      ON field_changes_log(nct_id, dept);
CREATE INDEX IF NOT EXISTS idx_field_changes_run_date ON field_changes_log(run_date);
CREATE INDEX IF NOT EXISTS idx_field_changes_gin      ON field_changes_log USING gin (curr_full_data);


-- ── 11. NCT Version Pairs ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS nct_version_pairs (
    id                  BIGSERIAL PRIMARY KEY,
    nct_id              TEXT  NOT NULL,
    dept                TEXT  NOT NULL REFERENCES departments(name),
    note                TEXT  DEFAULT '',
    total_versions      INTEGER DEFAULT 0,
    prev_version        INTEGER NOT NULL,
    curr_version        INTEGER NOT NULL,
    prev_date           TEXT,
    curr_date           TEXT,
    curr_status         TEXT,
    modules_changed     TEXT,
    field_change_count  INTEGER DEFAULT 0,
    field_changes       TEXT,
    curr_full_data      JSONB,
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (nct_id, dept, prev_version, curr_version)
);

CREATE INDEX IF NOT EXISTS idx_vpairs_nct  ON nct_version_pairs(nct_id, dept);
CREATE INDEX IF NOT EXISTS idx_vpairs_dept ON nct_version_pairs(dept, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_vpairs_gin  ON nct_version_pairs USING gin (curr_full_data);

ALTER TABLE nct_version_pairs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read nct_version_pairs"
    ON nct_version_pairs FOR SELECT USING (true);


-- ── 12. Canonical Changes Log ─────────────────────
CREATE TABLE IF NOT EXISTS canonical_changes_log (
    id              BIGSERIAL PRIMARY KEY,
    dept            TEXT     NOT NULL REFERENCES departments(name),
    run_date        DATE     NOT NULL,
    requested_nct   TEXT     NOT NULL,
    canonical_nct   TEXT     NOT NULL,
    already_tracked BOOLEAN  DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_canonical_run_date ON canonical_changes_log(dept, run_date);


-- ── 13. Run History ─────────────────────────────────────────────
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


-- ── 14. Department Keywords ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS dept_keywords (
    id          BIGSERIAL PRIMARY KEY,
    dept        TEXT  NOT NULL,
    drug_name   TEXT  NOT NULL,
    alias_names TEXT  DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_dept_keywords_dept ON dept_keywords(dept);


-- ── 15. Department General Terms ────────────────────────────────
CREATE TABLE IF NOT EXISTS dept_general_terms (
    id    BIGSERIAL PRIMARY KEY,
    dept  TEXT  NOT NULL,
    term  TEXT  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dept_general_terms_dept ON dept_general_terms(dept);


-- ── 16. Trial History ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trial_history (
    id              BIGSERIAL PRIMARY KEY,
    nct_id          TEXT  NOT NULL,
    dept            TEXT  NOT NULL,
    version_num     INTEGER,
    version_date    TEXT,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    overall_status  TEXT,
    modules_changed TEXT,
    field_changes   TEXT,
    full_data       JSONB,
    UNIQUE (nct_id, dept, version_num)
);

CREATE INDEX IF NOT EXISTS idx_history_nct  ON trial_history(nct_id, dept);
CREATE INDEX IF NOT EXISTS idx_history_ver  ON trial_history(nct_id, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_history_dept ON trial_history(dept, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_gin  ON trial_history USING gin (full_data);

ALTER TABLE trial_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read trial_history"
    ON trial_history FOR SELECT USING (true);


-- ── RPC: Unmatched deduplication ────────────────────────────────
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
