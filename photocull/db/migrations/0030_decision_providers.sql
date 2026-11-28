CREATE TABLE provider_selection (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tags TEXT NOT NULL CHECK (tags IN ('local', 'openai')),
    flags TEXT NOT NULL CHECK (flags IN ('local', 'openai'))
);

CREATE TABLE decision_jobs (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('tags', 'flags')),
    provider TEXT NOT NULL CHECK (provider IN ('local', 'openai')),
    content_hash TEXT NOT NULL,
    PRIMARY KEY (asset_id, kind)
) WITHOUT ROWID;

ALTER TABLE assets ADD COLUMN vlm_flag_provider TEXT
    CHECK (vlm_flag_provider IN ('openai', 'unjudged'));
ALTER TABLE assets ADD COLUMN vlm_quality TEXT CHECK (vlm_quality IS NULL OR json_valid(vlm_quality));

CREATE TABLE decision_tag_results (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('local', 'openai', 'unjudged')),
    content_hash TEXT NOT NULL,
    tag_key TEXT NOT NULL
);

CREATE INDEX decision_public_tags ON decision_tag_results(provider)
    WHERE provider IN ('openai', 'unjudged');

CREATE TABLE decision_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    score REAL NOT NULL CHECK (score BETWEEN -1 AND 1),
    PRIMARY KEY (asset_id, tag)
) WITHOUT ROWID;
CREATE INDEX decision_tags_by_tag ON decision_tags(tag, asset_id);

CREATE TRIGGER asset_invalidates_decisions AFTER UPDATE OF content_hash, active ON assets
WHEN OLD.content_hash <> NEW.content_hash OR (OLD.active = 1 AND NEW.active = 0)
BEGIN
    DELETE FROM decision_tags WHERE asset_id = NEW.id;
    DELETE FROM decision_tag_results WHERE asset_id = NEW.id;
    UPDATE assets SET vlm_flag_provider = NULL, vlm_quality = NULL
        WHERE id = NEW.id AND vlm_flag_provider IS NOT NULL;
    DELETE FROM decision_jobs WHERE asset_id = NEW.id;
END;

CREATE VIEW effective_tags AS
    SELECT t.asset_id, t.tag, t.score, e.tag_key, e.content_hash, 'local' AS provider
    FROM asset_tags AS t JOIN asset_embeddings AS e USING (asset_id)
    WHERE NOT EXISTS (SELECT 1 FROM decision_tag_results AS d
        WHERE d.asset_id = t.asset_id
        AND d.content_hash = e.content_hash AND d.tag_key = e.tag_key)
    UNION ALL
    SELECT t.asset_id, t.tag, t.score, d.tag_key, d.content_hash, d.provider
    FROM decision_tags AS t JOIN decision_tag_results AS d
        ON d.asset_id = t.asset_id;
