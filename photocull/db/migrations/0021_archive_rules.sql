CREATE TABLE archive_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    request TEXT NOT NULL,
    interval_hours INTEGER NOT NULL CHECK(interval_hours BETWEEN 0 AND 8760),
    next_at REAL,
    preview_id TEXT REFERENCES archive_previews(id),
    seen_preview_id TEXT,
    error TEXT
);
CREATE INDEX archive_rules_due ON archive_rules(next_at) WHERE next_at IS NOT NULL;
