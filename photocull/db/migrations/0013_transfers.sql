CREATE TABLE transfers (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('zip','copy','report')),
    status TEXT NOT NULL CHECK(status IN ('pending','running','cancelling','cancelled','done','error')),
    request TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    written_bytes INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT
) WITHOUT ROWID;
CREATE INDEX transfers_pending ON transfers(status, created_at, id);
