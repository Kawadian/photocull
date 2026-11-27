CREATE TABLE archive_write_checks (
    path TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    checked_at REAL NOT NULL
);
