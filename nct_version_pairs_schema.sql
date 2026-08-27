-- nct_version_pairs_schema.sql
-- Run in Supabase SQL Editor.
-- Stores every consecutive version pair comparison for each NCT.
-- One row per (nct_id, dept, prev_version, curr_version).

CREATE TABLE IF NOT EXISTS public.nct_version_pairs (
  id                 BIGSERIAL PRIMARY KEY,
  nct_id             TEXT NOT NULL,
  dept               TEXT NOT NULL,
  note               TEXT DEFAULT '',
  total_versions     INTEGER DEFAULT 0,
  prev_version       INTEGER NOT NULL,
  curr_version       INTEGER NOT NULL,
  prev_date          TEXT,
  curr_date          TEXT,
  curr_status        TEXT,
  modules_changed    TEXT,
  field_change_count INTEGER DEFAULT 0,
  field_changes      TEXT,
  fetched_at         TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (nct_id, dept, prev_version, curr_version)
);

CREATE INDEX IF NOT EXISTS idx_vpairs_nct  ON public.nct_version_pairs (nct_id, dept);
CREATE INDEX IF NOT EXISTS idx_vpairs_dept ON public.nct_version_pairs (dept, fetched_at DESC);

ALTER TABLE public.nct_version_pairs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read nct_version_pairs"
  ON public.nct_version_pairs FOR SELECT
  USING (true);
