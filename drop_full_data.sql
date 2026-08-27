-- Run this in Supabase SQL Editor AFTER reparse_all.py finishes successfully.
-- Drops the now-redundant full_data JSONB column.
-- All 62 fields are now stored as individual columns.

ALTER TABLE organized_trials DROP COLUMN IF EXISTS full_data;
