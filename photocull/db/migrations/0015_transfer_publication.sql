ALTER TABLE transfers ADD COLUMN output_device INTEGER;
ALTER TABLE transfers ADD COLUMN output_inode INTEGER;
CREATE INDEX transfers_history ON transfers(
    (kind = 'report'), (status IN ('pending','running','cancelling')) DESC, created_at DESC, id DESC
);
