-- Keep group assignments and the dirty queue authoritative. The native index
-- on disk is a disposable checkpoint of the SQLite anchor set.
ALTER TABLE ml_state ADD COLUMN anchor_revision TEXT NOT NULL DEFAULT '';
CREATE TABLE similar_anchors (asset_id INTEGER PRIMARY KEY REFERENCES assets(id));
CREATE TABLE similar_dirty (asset_id INTEGER PRIMARY KEY REFERENCES assets(id));
CREATE TABLE similar_invalid_anchors (asset_id INTEGER PRIMARY KEY REFERENCES assets(id));

-- Phase 3 databases have groups but no durable singleton assignments. Build
-- the anchor set once during the upgrade, keeping vectors, scores and tags.
DELETE FROM similar_groups;
UPDATE ml_state SET grouped_generation = -1, anchor_revision = lower(hex(randomblob(16)));
INSERT INTO similar_dirty SELECT asset_id FROM asset_embeddings;

DROP TRIGGER embedding_insert;
DROP TRIGGER embedding_delete;

CREATE TRIGGER embedding_insert AFTER INSERT ON asset_embeddings BEGIN
    UPDATE ml_state SET generation = generation + 1;
    INSERT INTO similar_dirty VALUES (NEW.asset_id) ON CONFLICT DO NOTHING;
    INSERT INTO burst_changes SELECT captured_at FROM assets
        WHERE id = NEW.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
END;

CREATE TRIGGER embedding_delete AFTER DELETE ON asset_embeddings BEGIN
    UPDATE ml_state SET generation = generation + 1;
    INSERT INTO similar_invalid_anchors SELECT asset_id FROM similar_anchors
        WHERE asset_id = OLD.asset_id ON CONFLICT DO NOTHING;
    INSERT INTO similar_dirty SELECT asset_id FROM similar_members
        WHERE group_id = OLD.asset_id ON CONFLICT DO NOTHING;
    INSERT INTO similar_dirty VALUES (OLD.asset_id) ON CONFLICT DO NOTHING;

    DELETE FROM similar_groups WHERE id = OLD.asset_id OR (member_count = 2 AND id = (
        SELECT group_id FROM similar_members WHERE asset_id = OLD.asset_id
    ));
    UPDATE similar_groups SET member_count = member_count - 1 WHERE id = (
        SELECT group_id FROM similar_members WHERE asset_id = OLD.asset_id
    );
    DELETE FROM similar_members WHERE asset_id = OLD.asset_id;

    INSERT INTO burst_changes SELECT captured_at FROM assets
        WHERE id = OLD.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
END;
