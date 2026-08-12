CREATE TABLE cdp.exposure_attribution AS
WITH prepared AS (
  SELECT
    e.*,
    CASE WHEN jsonb_typeof(e.payload_json->'bucket') = 'number'
               AND e.payload_json->>'bucket' ~ '^-?[0-9]+$'
      THEN (e.payload_json->>'bucket')::integer END AS bucket
  FROM cdp.exposure_event_raw e
),
candidate AS (
  SELECT
    p.event_id, p.tenant_id, p.user_id_hash, a.allocation_id,
    a.experiment_path::text AS experiment_path, a.variant,
    p.occurred_at_utc AS matched_at_utc, p.bucket,
    'path_window_bucket_consent'::text AS match_reason,
    row_number() OVER (
      PARTITION BY p.event_id
      ORDER BY nlevel(a.experiment_path) DESC, a.priority DESC, a.allocation_id
    ) AS rn
  FROM prepared p
  JOIN cdp.tenant_account t
    ON t.tenant_id = p.tenant_id
   AND t.status = 'active'
  JOIN cdp.consent_snapshot c
    ON c.tenant_id = p.tenant_id
   AND c.user_id_hash = p.user_id_hash
   AND p.occurred_at_utc <@ c.consent_window
   AND c.allow_experiment
  JOIN cdp.experiment_allocation a
    ON a.tenant_id = p.tenant_id
   AND a.enabled
   AND p.occurred_at_utc <@ a.active_window
   AND p.surface_path <@ a.experiment_path
   AND p.bucket <@ a.bucket_window
  WHERE p.bucket IS NOT NULL
)
SELECT event_id, tenant_id, user_id_hash, allocation_id, experiment_path,
       variant, matched_at_utc, bucket, match_reason
FROM candidate
WHERE rn = 1;

CREATE TABLE cdp.rejected_exposure AS
WITH prepared AS (
  SELECT
    e.*,
    CASE WHEN jsonb_typeof(e.payload_json->'bucket') = 'number'
               AND e.payload_json->>'bucket' ~ '^-?[0-9]+$'
      THEN (e.payload_json->>'bucket')::integer END AS bucket
  FROM cdp.exposure_event_raw e
),
reasoned AS (
  SELECT
    p.event_id,
    p.tenant_id,
    CASE
      WHEN t.status = 'disabled' THEN 'tenant_disabled'
      WHEN jsonb_typeof(p.payload_json->'bucket') <> 'number'
        OR p.payload_json->>'bucket' !~ '^-?[0-9]+$'
        OR jsonb_typeof(p.payload_json->'bucket') IS NULL THEN 'malformed_payload'
      WHEN NOT EXISTS (
        SELECT 1 FROM cdp.consent_snapshot c
        WHERE c.tenant_id = p.tenant_id
          AND c.user_id_hash = p.user_id_hash
          AND p.occurred_at_utc <@ c.consent_window
          AND c.allow_experiment
      ) THEN 'consent_denied'
      WHEN NOT EXISTS (
        SELECT 1 FROM cdp.experiment_allocation a
        WHERE a.tenant_id = p.tenant_id
          AND a.enabled
          AND p.surface_path <@ a.experiment_path
      ) THEN 'path_unmatched'
      WHEN NOT EXISTS (
        SELECT 1 FROM cdp.experiment_allocation a
        WHERE a.tenant_id = p.tenant_id
          AND a.enabled
          AND p.surface_path <@ a.experiment_path
          AND p.occurred_at_utc <@ a.active_window
      ) THEN 'outside_window'
      ELSE 'bucket_unmatched'
    END AS reject_reason,
    CASE
      WHEN t.status = 'disabled' THEN 'tenant status is disabled before attribution'
      WHEN jsonb_typeof(p.payload_json->'bucket') <> 'number'
        OR p.payload_json->>'bucket' !~ '^-?[0-9]+$'
        OR jsonb_typeof(p.payload_json->'bucket') IS NULL THEN 'payload_json has no integer bucket field'
      WHEN NOT EXISTS (
        SELECT 1 FROM cdp.consent_snapshot c
        WHERE c.tenant_id = p.tenant_id
          AND c.user_id_hash = p.user_id_hash
          AND p.occurred_at_utc <@ c.consent_window
          AND c.allow_experiment
      ) THEN 'allow_experiment=false at event time'
      WHEN NOT EXISTS (
        SELECT 1 FROM cdp.experiment_allocation a
        WHERE a.tenant_id = p.tenant_id
          AND a.enabled
          AND p.surface_path <@ a.experiment_path
      ) THEN 'surface_path is not under an active experiment_path'
      WHEN NOT EXISTS (
        SELECT 1 FROM cdp.experiment_allocation a
        WHERE a.tenant_id = p.tenant_id
          AND a.enabled
          AND p.surface_path <@ a.experiment_path
          AND p.occurred_at_utc <@ a.active_window
      ) THEN 'matching allocation closed before event time'
      ELSE 'bucket is outside every int4range allocation bucket'
    END AS detail
  FROM prepared p
  LEFT JOIN cdp.tenant_account t ON t.tenant_id = p.tenant_id
)
SELECT r.event_id, r.tenant_id, r.reject_reason, r.detail
FROM reasoned r
WHERE NOT EXISTS (
  SELECT 1 FROM cdp.exposure_attribution a WHERE a.event_id = r.event_id
);

CREATE TABLE cdp.path_variant_rollup AS
SELECT tenant_id, experiment_path, variant,
       count(*)::integer AS exposure_count,
       count(DISTINCT user_id_hash)::integer AS distinct_users
FROM cdp.exposure_attribution
GROUP BY tenant_id, experiment_path, variant;
