ALTER TABLE cdp.exposure_attribution ENABLE ROW LEVEL SECURITY;
ALTER TABLE cdp.exposure_attribution FORCE ROW LEVEL SECURITY;

DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_video_ro') THEN
    CREATE ROLE tenant_video_ro NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_shop_ro') THEN
    CREATE ROLE tenant_shop_ro NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_auditor') THEN
    CREATE ROLE platform_auditor NOLOGIN;
  END IF;
END
$roles$;

CREATE POLICY exposure_attribution_tenant_ro
ON cdp.exposure_attribution
FOR SELECT
USING (
  session_user = current_user
  AND (
    current_user = 'platform_auditor'
    OR (
      current_user = 'tenant_video_ro'
      AND current_setting('app.tenant_id', true) = 'video'
      AND tenant_id = 'video'
    )
    OR (
      current_user = 'tenant_shop_ro'
      AND current_setting('app.tenant_id', true) = 'shop'
      AND tenant_id = 'shop'
    )
  )
);

GRANT USAGE ON SCHEMA cdp TO tenant_video_ro, tenant_shop_ro, platform_auditor;
GRANT SELECT ON cdp.exposure_attribution TO tenant_video_ro, tenant_shop_ro, platform_auditor;
