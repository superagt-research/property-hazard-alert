-- DESIGN ONLY. Not executed. Requires reviewed migrations and an existing DB.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE ingestion_run (
  run_id uuid PRIMARY KEY, source text NOT NULL, started_at timestamptz NOT NULL,
  completed_at timestamptz, status text NOT NULL, inventory_scope jsonb NOT NULL,
  inventory_complete boolean NOT NULL DEFAULT false,
  enrichment_complete boolean NOT NULL DEFAULT false, manifest_s3_uri text,
  metrics jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE event_revision (
  revision_id uuid PRIMARY KEY, source text NOT NULL, source_record_id text NOT NULL,
  source_revision_hash text NOT NULL, schema_version text NOT NULL,
  ingested_at timestamptz NOT NULL, issued_at timestamptz, source_updated_at timestamptz,
  effective_at timestamptz, expires_at timestamptz, onset_at timestamptz, event_ends_at timestamptz,
  source_lifecycle text NOT NULL, evidence_type text NOT NULL, hazard_types text[] NOT NULL,
  attributes jsonb NOT NULL, raw_s3_uri text NOT NULL,
  UNIQUE(source,source_record_id,source_revision_hash)
);
CREATE TABLE event_geometry (
  geometry_id uuid PRIMARY KEY, revision_id uuid NOT NULL REFERENCES event_revision,
  role text NOT NULL, valid_at timestamptz, boundary_version text,
  geom geometry(Geometry,4326), original_geometry_s3_uri text,
  quality_flags text[] NOT NULL, geometry_complete boolean NOT NULL
);
CREATE INDEX event_geometry_gist ON event_geometry USING gist(geom);
CREATE TABLE event_current (
  source text NOT NULL, source_record_id text NOT NULL,
  revision_id uuid NOT NULL REFERENCES event_revision, published_run_id uuid NOT NULL REFERENCES ingestion_run,
  missing_from_latest_snapshot boolean NOT NULL DEFAULT false,
  PRIMARY KEY(source,source_record_id)
);
CREATE TABLE moratorium_policy_revision (
  policy_revision_id uuid PRIMARY KEY, logical_policy_id uuid NOT NULL, version integer NOT NULL,
  status text NOT NULL, product_ids text[] NOT NULL, coverage_perils text[] NOT NULL,
  transaction_types text[] NOT NULL, valid_from timestamptz NOT NULL, valid_to timestamptz,
  scope geometry(Geometry,4326), rule jsonb NOT NULL,
  approved_by text, approved_at timestamptz, reason text NOT NULL,
  UNIQUE(logical_policy_id,version)
);
CREATE TABLE underwriting_decision_audit (
  decision_id uuid PRIMARY KEY, idempotency_key text NOT NULL, action text NOT NULL,
  evaluated_at timestamptz NOT NULL, location_version text NOT NULL, product_version text NOT NULL,
  snapshot_run_ids uuid[] NOT NULL, event_revision_ids uuid[] NOT NULL, policy_revision_ids uuid[] NOT NULL,
  mode text NOT NULL, outcome text NOT NULL, reasons jsonb NOT NULL, source_health jsonb NOT NULL,
  actor_id text NOT NULL, override_id uuid,
  UNIQUE(idempotency_key,action)
);
-- Authoritative point/footprint matching must also enforce source freshness,
-- policy time/scope, evidence roles, unresolved areas and location quality.
-- ST_Intersects includes boundary points. Do not match arbitrary Point/cone
-- geometries as if they were active damaging wind/flood footprints.
-- Distance screening uses ST_DWithin(geom::geography, point::geography, metres).
-- Split/normalize antimeridian geometry and validate with ST_IsValid before use.
