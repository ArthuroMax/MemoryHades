-- Target schema for a Postgres deployment (documentation: server.py currently implements SQLite only).
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL);
CREATE TABLE nav_records (
    id BIGSERIAL PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    map_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('hop','route','stuck','escape','coin')),
    cell_x INTEGER NOT NULL, cell_z INTEGER NOT NULL, y DOUBLE PRECISION NOT NULL DEFAULT 0,
    successes DOUBLE PRECISION NOT NULL DEFAULT 0, failures DOUBLE PRECISION NOT NULL DEFAULT 0,
    abandons DOUBLE PRECISION NOT NULL DEFAULT 0, unsafe DOUBLE PRECISION NOT NULL DEFAULT 0,
    obs_count INTEGER NOT NULL DEFAULT 0, last_reason TEXT NOT NULL DEFAULT '', map_sig TEXT NOT NULL DEFAULT '',
    first_seen BIGINT NOT NULL, last_seen BIGINT NOT NULL, last_unsafe BIGINT,
    UNIQUE (map_id, kind, cell_x, cell_z)
);
CREATE TABLE record_sources (record_id BIGINT NOT NULL REFERENCES nav_records(id) ON DELETE CASCADE, source_hash TEXT NOT NULL, PRIMARY KEY (record_id, source_hash));
CREATE TABLE nav_observations (
    id BIGSERIAL PRIMARY KEY, schema_version INTEGER NOT NULL, received_at BIGINT NOT NULL,
    record_id BIGINT NOT NULL REFERENCES nav_records(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('success','fail','abandon','unsafe')), reason TEXT NOT NULL, nav_state TEXT NOT NULL,
    recovery TEXT NOT NULL, escape_reached BOOLEAN NOT NULL, source_hash TEXT NOT NULL
);
CREATE INDEX idx_records_map ON nav_records (map_id);
