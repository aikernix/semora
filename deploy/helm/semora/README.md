# Semora Helm Chart

This chart deploys Semora application workloads to an existing banking Kubernetes platform. It intentionally does not bundle PostgreSQL, Keycloak, S3-compatible storage or an observability backend. Those services and their backup, HA, KMS and lifecycle policies remain bank-managed. Set `telemetry.otlpEndpoint` to the bank OpenTelemetry Collector; use an empty value to disable export.

Before installation, publish the eight Semora images with immutable tags and create the secrets named in `values.yaml`. Migration secrets must use schema-owner/DDL accounts; runtime secrets must use separate least-privilege accounts. `semora-datasources` is injected only into Executor and contains `BUSINESS_DATABASE_URL` and/or `DATASOURCE_<SECRET_REF>_URL` entries. No other workload receives business database credentials.

The metadata database must pre-provision both `database.apiRole` and `database.auditExporterRole`. The audit exporter role receives only read access to completed runs/events plus insert access to the immutable archive registry. `auditArchive.objectStoreSecret` must reference a versioned bucket created with S3 Object Lock enabled when `wormMode` is `governance` or `compliance`; the default is `compliance`. Both object-store secrets must contain `kmsKeyId`; production defaults require `aws:kms`, bucket versioning and per-object encryption verification for knowledge sources and audit archives. Startup fails closed when these controls are missing. An optional Kafka secret may contain `AUDIT_KAFKA_*` settings. Kafka messages contain only the archive pointer and hashes, never the audit event body.

```bash
helm lint deploy/helm/semora
helm template semora deploy/helm/semora --namespace semora
helm upgrade --install semora deploy/helm/semora \
  --namespace semora --create-namespace -f bank-values.yaml
```

Pre-install/pre-upgrade hooks run metadata, LangGraph checkpointer, ontology and knowledge migrations with demo seeding disabled. The web runtime configuration is mounted as `semora-config.js`, so OIDC endpoints can change without rebuilding the SPA image.
