ALTER TABLE scan_requests ADD COLUMN files_indexed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scan_requests ADD COLUMN bytes_hashed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scan_requests ADD COLUMN file_errors INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scan_requests ADD COLUMN directory_errors INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scan_requests ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE assets ADD COLUMN scan_attempt INTEGER NOT NULL DEFAULT 0;

CREATE TABLE scan_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scan_requests(id),
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'directory')),
    operation TEXT NOT NULL CHECK (operation IN ('list', 'stat', 'read')),
    code TEXT NOT NULL CHECK (code IN ('permission_denied', 'missing', 'changed', 'io_error')),
    errno INTEGER,
    source_key TEXT
);
CREATE INDEX scan_errors_page ON scan_errors (scan_id, id);

CREATE INDEX jobs_pending_media ON jobs (asset_id, stage)
    WHERE status = 'pending' AND stage <> 'embedding';

CREATE TABLE preview_cleanup (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (asset_id, content_hash)
) WITHOUT ROWID;

CREATE TRIGGER assets_preview_cleanup AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash BEGIN
    INSERT INTO preview_cleanup (asset_id, content_hash) VALUES (OLD.id, OLD.content_hash)
        ON CONFLICT (asset_id, content_hash) DO NOTHING;
END;
