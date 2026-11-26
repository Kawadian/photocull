CREATE TABLE story_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    key TEXT,
    error TEXT
);
INSERT INTO story_state(id) VALUES (1);

CREATE TABLE story_dirty (
    day TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE story_days (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    segments INTEGER NOT NULL,
    key TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE story_segments (
    day TEXT NOT NULL REFERENCES story_days(day) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT,
    started TEXT,
    ended TEXT,
    latitude REAL,
    longitude REAL,
    count INTEGER NOT NULL,
    representatives TEXT NOT NULL,
    PRIMARY KEY (day, position)
) WITHOUT ROWID;

CREATE TRIGGER story_asset_insert AFTER INSERT ON assets
WHEN (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) VALUES(NEW.day)
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_asset_update
AFTER UPDATE OF active, day, captured_at, latitude, longitude, rating, rejected ON assets
WHEN (SELECT key IS NOT NULL FROM story_state) AND (
    OLD.active <> NEW.active OR OLD.day <> NEW.day OR OLD.rating <> NEW.rating
    OR OLD.rejected <> NEW.rejected OR OLD.captured_at IS NOT NEW.captured_at
    OR OLD.latitude IS NOT NEW.latitude OR OLD.longitude IS NOT NEW.longitude
)
BEGIN
    INSERT INTO story_dirty(day) VALUES(OLD.day), (NEW.day)
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_aesthetic AFTER INSERT ON insight_results
WHEN NEW.stage = 'aesthetic' AND NEW.score IS NOT NULL
 AND (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_score_insert AFTER INSERT ON asset_scores
WHEN (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_score_update AFTER UPDATE ON asset_scores
WHEN (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;


CREATE TRIGGER story_preview AFTER UPDATE OF status ON jobs
WHEN NEW.stage = 'preview' AND OLD.status <> NEW.status AND (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_burst_insert AFTER INSERT ON burst_members
WHEN (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_burst_delete AFTER DELETE ON burst_members
WHEN (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = OLD.asset_id
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;
