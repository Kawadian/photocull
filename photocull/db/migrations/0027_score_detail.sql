ALTER TABLE asset_scores ADD COLUMN detail REAL;
ALTER TABLE asset_scores ADD COLUMN bright_p99 REAL;
ALTER TABLE asset_scores ADD COLUMN dark_p01 REAL;

UPDATE analysis_state SET scoring_settings = '';
