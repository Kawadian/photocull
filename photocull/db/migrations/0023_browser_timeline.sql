ALTER TABLE assets ADD COLUMN timeline_time TEXT
    GENERATED ALWAYS AS (coalesce(captured_at, '')) VIRTUAL;

CREATE INDEX asset_timeline ON assets (day, timeline_time, id) WHERE active = 1;
