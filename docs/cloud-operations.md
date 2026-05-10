# Fisherman Cloud Operations

This is for repository operators, not normal users.

## Deployment

The managed Cloud path is CI/CD driven:

- `docker-publish.yml` builds and publishes the image.
- `deploy-cvm.yml` upgrades the Phala CVM on `main`.
- `bootstrap-cvm.yml` creates a CVM when needed.
- `attestation-monitor.yml` audits the live endpoint hourly.

Required secrets and variables are listed in [SETUP.md](../SETUP.md).

## User Enrollment

Hosted Cloud can run in three modes:

- `FISH_CLOUD_ENROLLMENT_MODE=open`: valid FishKey identities auto-enroll.
- `FISH_CLOUD_ENROLLMENT_MODE=allowlist`: only `FISH_CLOUD_ALLOWED_PUBKEYS` auto-enroll.
- `FISH_CLOUD_ENROLLMENT_MODE=closed`: users can request access and remain pending.

Users request access through:

```bash
fisherman cloud request-access
fisherman cloud account
```

The backend records pending users in `users.enrollment_state='pending'`.
Until an operator approves them, clients keep raw uploads queued locally.

## Approval

For invite/dogfood operation, approve users by changing their row to:

```sql
UPDATE users
SET enrollment_state = 'active',
    enrollment_approved_at = now(),
    plan = 'dogfood',
    max_frames_per_hour = COALESCE(max_frames_per_hour, 1200)
WHERE user_pubkey = '<hex pubkey>';
```

Do not approve unknown public keys. Public keys identify tenant
namespaces, but FishKey signatures still prove possession of the private
key for writes and reads.

## Key Rotation

Rotate any token that appears in chat, shell history, CI logs, issues, or
PRs. In particular, Phala API keys used by `PHALA_CLOUD_API_KEY` should
be treated as deployment-control credentials.

## Release Approval UX

When CI deploys a new compose hash, strict clients do not silently upload
raw context. Users must approve the new Cloud release in Settings or run:

```bash
fisherman cloud audit https://fisherman.teleport.computer
fisherman backend configure cloud
```

If they do not approve, capture continues locally and the upload queue
holds frames for later.
