ALTER TABLE assets ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1));

CREATE TABLE browser_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    name_key TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (asset_id, name_key)
) WITHOUT ROWID;

CREATE INDEX browser_tag_assets ON browser_tags (name_key, asset_id);
CREATE INDEX browser_favorites ON assets (day, timeline_time, id)
    WHERE active = 1 AND favorite = 1;
CREATE INDEX browser_kind_timeline ON assets (kind, day, timeline_time, id) WHERE active = 1;
