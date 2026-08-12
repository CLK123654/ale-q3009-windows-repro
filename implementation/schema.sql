CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA cdp;

CREATE TABLE cdp.tenant_account (
  tenant_id text PRIMARY KEY,
  tenant_name text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','disabled')),
  owner_team text NOT NULL
);

CREATE TABLE cdp.experiment_allocation (
  allocation_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES cdp.tenant_account(tenant_id),
  experiment_path ltree NOT NULL,
  variant text NOT NULL,
  starts_at_utc timestamptz NOT NULL,
  ends_at_utc timestamptz NOT NULL,
  bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 100),
  bucket_end integer NOT NULL CHECK (bucket_end > bucket_start AND bucket_end <= 100),
  priority integer NOT NULL,
  enabled boolean NOT NULL,
  active_window tstzrange GENERATED ALWAYS AS (tstzrange(starts_at_utc, ends_at_utc, '[)')) STORED,
  bucket_window int4range GENERATED ALWAYS AS (int4range(bucket_start, bucket_end, '[)')) STORED,
  CHECK (starts_at_utc < ends_at_utc),
  EXCLUDE USING gist (
    tenant_id WITH =,
    experiment_path WITH =,
    active_window WITH &&,
    bucket_window WITH &&
  ) WHERE (enabled)
);

CREATE TABLE cdp.consent_snapshot (
  tenant_id text NOT NULL REFERENCES cdp.tenant_account(tenant_id),
  user_id_hash text NOT NULL,
  consent_start_utc timestamptz NOT NULL,
  consent_end_utc timestamptz NOT NULL,
  allow_experiment boolean NOT NULL,
  consent_window tstzrange GENERATED ALWAYS AS (tstzrange(consent_start_utc, consent_end_utc, '[)')) STORED,
  PRIMARY KEY (tenant_id, user_id_hash, consent_start_utc)
);

CREATE TABLE cdp.exposure_event_raw (
  event_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  user_id_hash text NOT NULL,
  occurred_at_utc timestamptz NOT NULL,
  surface_path ltree NOT NULL,
  payload_json jsonb NOT NULL
);

CREATE INDEX exposure_raw_surface_time_gist
  ON cdp.exposure_event_raw USING gist (tenant_id, surface_path, occurred_at_utc);
CREATE INDEX allocation_path_time_gist
  ON cdp.experiment_allocation USING gist (tenant_id, experiment_path, active_window, bucket_window);
CREATE INDEX exposure_payload_bucket_idx
  ON cdp.exposure_event_raw ((
    CASE
      WHEN jsonb_typeof(payload_json->'bucket') = 'number'
       AND payload_json->>'bucket' ~ '^-?[0-9]+$'
      THEN (payload_json->>'bucket')::integer
    END
  ));
