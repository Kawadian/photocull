ALTER TABLE transfers ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;

CREATE TABLE transfer_skips (
    transfer_id TEXT NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (transfer_id, asset_id)
) WITHOUT ROWID;
