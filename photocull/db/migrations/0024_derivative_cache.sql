CREATE TABLE derivative_files (
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    variant TEXT NOT NULL,
    version TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('hot', 'large')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'ready', 'error')),
    priority INTEGER NOT NULL CHECK (priority IN (0, 1)),
    requested_at REAL NOT NULL,
    last_access REAL NOT NULL,
    byte_size INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (asset_id, variant, version)
) WITHOUT ROWID;

CREATE INDEX derivative_pending ON derivative_files (priority DESC, requested_at)
    WHERE status = 'pending';
CREATE INDEX derivative_lru ON derivative_files (tier, last_access)
    WHERE status = 'ready';

CREATE TABLE derivative_usage (
    tier TEXT PRIMARY KEY,
    byte_size INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
INSERT INTO derivative_usage(tier) VALUES ('hot'), ('large');

CREATE TRIGGER derivative_insert AFTER INSERT ON derivative_files WHEN NEW.byte_size > 0 BEGIN
    UPDATE derivative_usage SET byte_size = byte_size + NEW.byte_size WHERE tier = NEW.tier;
END;
CREATE TRIGGER derivative_resize AFTER UPDATE OF byte_size ON derivative_files
WHEN OLD.byte_size <> NEW.byte_size BEGIN
    UPDATE derivative_usage
    SET byte_size = byte_size + NEW.byte_size - OLD.byte_size
    WHERE tier = NEW.tier;
END;
CREATE TRIGGER derivative_delete AFTER DELETE ON derivative_files WHEN OLD.byte_size > 0 BEGIN
    UPDATE derivative_usage SET byte_size = byte_size - OLD.byte_size WHERE tier = OLD.tier;
END;

CREATE TABLE derivative_placeholders (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
    version TEXT NOT NULL,
    blurhash TEXT NOT NULL
);
