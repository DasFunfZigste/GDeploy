# Architecture

```mermaid
flowchart LR
  Browser[Authenticated browser] --> API[FastAPI and static UI]
  API --> DB[(SQLite accounts, trust, jobs and events)]
  API --> Vault[Encrypted credentials]
  DB --> Worker[One deployment worker]
  Media[Read-only vendor media] --> Builder[xorriso autoinstall builder]
  Worker --> Builder
  Worker -->|HTTPS API and upload| ESXi[Standalone ESXi 8.0 U3]
  ESXi --> Ubuntu[Separate Ubuntu 24.04 VMs]
  Worker -->|SSH| Ubuntu
  Kibana[Kibana VM] -->|Service token and verified TLS| Elastic[Elasticsearch VM]
  Splunk[Splunk VM]
```

Deployment stages: queued → preflight → preparing media → creating VM → installing OS → installing software → verifying → completed. An exception moves the job to failed and preserves its error/events. Shutdown during work becomes interrupted on restart. Queued jobs survive restart; running jobs are deliberately not resumed because an external operation may have completed without a local acknowledgment.

The specification, generated credentials and ESXi connection snapshot are saved before provisioning. The remote ISO path is recorded before upload. A VM receives its UUID ownership annotation atomically as part of creation; cleanup can discover that annotation if a crash precedes saving the managed-object ID. Each mutation persists relevant state before moving to the next stage.

Recovery: failed/interrupted → cleaning → reverted + new queued deployment. A cleanup or replacement-preflight error becomes cleanup_failed and can be retried. Cleanup only deletes tagged VMs with deployment-owned storage and explicitly recorded ISO paths. It never deletes a datastore or network.

The app is a single-administrator tool. Auth uses scrypt password hashes, random server-side sessions with hashed cookie identifiers, SameSite/HttpOnly cookies and CSRF tokens. Secrets use authenticated Fernet encryption; ordinary API responses omit secrets. The encryption key is external to the database. Credential reveals are POST requests with CSRF verification, no-store headers and an audit event.

The HTTPS deployment connection snapshot is encrypted with each job so changing Settings does not redirect existing work or recovery to a different ESXi host. Changed credentials for the same host may require operator intervention if an old job's snapshot is no longer valid.

ESXi certificate retrieval returns the peer certificate's subject, issuer, SHA-256 fingerprint, validity dates and subject alternative names for operator review. The administrator explicitly approves the certificate for that exact host endpoint after comparing its fingerprint with an independently trusted ESXi source. The fetched certificate by itself is not proof of host identity.

Approved certificates are persisted in an additive SQLite trust table. Each new ESXi client looks up the current approval for the endpoint it will connect to, including the host saved in an older deployment snapshot. Certificate approvals are not copied into deployment snapshots. Both the API transport and datastore HTTPS uploader enforce the exact approved leaf certificate and its valid dates on their connections. Endpoint-specific approval permits a hostname/SAN mismatch; without an approval, normal system/private CA and hostname verification applies. A changed or renewed certificate requires explicit review and replacement of the approval.

Trust removal restores CA-based verification. Trust updates affect subsequent connections; existing authenticated connections may finish with their already-verified certificate. Container restart is unnecessary, and approved certificates survive restart in the same app data volume. Backups must include the trust table. Application versions before **0.2.0** ignore the table and use their previous TLS settings, which can include a saved `verify_tls=false`. Before rollback, review those settings and ensure certificate verification is enabled and trusts the intended host.

An actual host/guest lab is required to establish integration compatibility. No simulated deployment mode is exposed to users, and production success is only reported after OS and application checks return successfully.
