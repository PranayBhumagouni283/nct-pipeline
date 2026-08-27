-- trial_history_schema.sql
-- Run in Supabase SQL Editor to create the historical versions table.
-- Stores every version snapshot detected by Flow 2 going forward,
-- plus any backfilled versions added via backfill_history.py.

CREATE TABLE IF NOT EXISTS public.trial_history (
  id              BIGSERIAL PRIMARY KEY,
  nct_id          TEXT NOT NULL,
  dept            TEXT NOT NULL,
  version_num     INTEGER,
  version_date    TEXT,
  fetched_at      TIMESTAMPTZ DEFAULT NOW(),
  overall_status  TEXT,
  modules_changed TEXT,
  field_changes   TEXT,
  full_data       JSONB,
  UNIQUE (nct_id, dept, version_num)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_history_nct   ON public.trial_history (nct_id, dept);
CREATE INDEX IF NOT EXISTS idx_history_ver   ON public.trial_history (nct_id, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_history_dept  ON public.trial_history (dept, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_gin   ON public.trial_history USING gin (full_data);

-- Enable Row Level Security (matching other tables in the project)
ALTER TABLE public.trial_history ENABLE ROW LEVEL SECURITY;

-- Public read policy (same as organized_trials)
CREATE POLICY "Public read trial_history"
  ON public.trial_history FOR SELECT
  USING (true);
