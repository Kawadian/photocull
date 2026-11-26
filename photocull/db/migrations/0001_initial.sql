CREATE TABLE scan_requests (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'error')),
    created_at REAL NOT NULL,
    finished_at REAL,
    error TEXT
);

CREATE UNIQUE INDEX scan_one_active ON scan_requests ((1))
    WHERE status IN ('pending', 'running');

CREATE TABLE assets (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'raw', 'heif', 'video')),
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    scan_id INTEGER NOT NULL REFERENCES scan_requests(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    day TEXT NOT NULL,
    captured_at TEXT,
    make TEXT,
    model TEXT,
    latitude REAL,
    longitude REAL,
    metadata_error TEXT,
    width INTEGER,
    height INTEGER,
    duration REAL,
    rating INTEGER NOT NULL DEFAULT 0 CHECK (rating BETWEEN 0 AND 5),
    rejected INTEGER NOT NULL DEFAULT 0 CHECK (rejected IN (0, 1) AND (rejected = 0 OR rating = 0)),
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX asset_folder ON assets (folder, id) WHERE active = 1;
CREATE INDEX asset_day ON assets (day, id) WHERE active = 1;
CREATE INDEX asset_active ON assets (id) WHERE active = 1;

CREATE TABLE jobs (
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    stage TEXT NOT NULL CHECK (stage IN ('exif', 'preview')),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'error', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (asset_id, stage)
);

CREATE INDEX jobs_pending ON jobs (asset_id, stage) WHERE status = 'pending';

CREATE TABLE stage_counts (
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (stage, status)
);

CREATE TRIGGER jobs_insert AFTER INSERT ON jobs BEGIN
    INSERT INTO stage_counts (stage, status, count) VALUES (NEW.stage, NEW.status, 1)
        ON CONFLICT (stage, status) DO UPDATE SET count = count + 1;
END;

CREATE TRIGGER jobs_update AFTER UPDATE OF status ON jobs WHEN OLD.status <> NEW.status BEGIN
    UPDATE stage_counts SET count = count - 1 WHERE stage = OLD.stage AND status = OLD.status;
    INSERT INTO stage_counts (stage, status, count) VALUES (NEW.stage, NEW.status, 1)
        ON CONFLICT (stage, status) DO UPDATE SET count = count + 1;
END;

CREATE TRIGGER jobs_delete AFTER DELETE ON jobs BEGIN
    UPDATE stage_counts SET count = count - 1 WHERE stage = OLD.stage AND status = OLD.status;
END;

CREATE TABLE worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat REAL NOT NULL,
    pid INTEGER NOT NULL
);
