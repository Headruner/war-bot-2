
CREATE TABLE IF NOT EXISTS characters (
    character_id   INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    tags           TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'OK',      -- OK | LIMITED | REAUTH | DISABLED
    scopes         TEXT NOT NULL DEFAULT '[]',
    owner_hash     TEXT,
    demo           INTEGER NOT NULL DEFAULT 0,
    added_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    character_id      INTEGER PRIMARY KEY REFERENCES characters(character_id) ON DELETE CASCADE,
    client_id         TEXT NOT NULL,
    access_token      BLOB,
    access_expires_at REAL NOT NULL DEFAULT 0,
    refresh_token     BLOB NOT NULL,
    refresh_tail      TEXT NOT NULL DEFAULT '',
    updated_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    character_id    INTEGER PRIMARY KEY REFERENCES characters(character_id) ON DELETE CASCADE,
    solar_system_id INTEGER,
    station_id      INTEGER,
    structure_id    INTEGER,
    ship_type_id    INTEGER,
    ship_item_id    INTEGER,
    ship_name       TEXT,
    online          INTEGER,
    last_login      TEXT,
    last_logout     TEXT,
    fetched_at      REAL,
    error           TEXT
);
CREATE TABLE IF NOT EXISTS type_names (
    type_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_history (
    solar_system_id INTEGER PRIMARY KEY,
    used_at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_states (
    state      TEXT PRIMARY KEY,
    purpose    TEXT NOT NULL,          -- login | add
    created_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS launches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    stopped_at   TEXT,
    version      TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    mode         TEXT NOT NULL,
    python       TEXT NOT NULL,
    platform     TEXT NOT NULL,
    cpu_count    INTEGER,
    rss_mb_start REAL,
    rss_mb_peak  REAL,
    requests     INTEGER,
    config       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
