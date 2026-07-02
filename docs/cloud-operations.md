# Fisherman Cloud Operations

This is for repository operators, not normal users.

## Deployment

Fisherman Cloud now runs on the EC2-backed service behind
`https://fisherman.teleport.computer`.

The active deployment pieces are:

- EC2 instance running `fisherman-ingest.service` for `/ingest` and the HTTP API.
- EC2 instance running `fisherman-relay.service` for the E2EE relay.
- nginx with Let's Encrypt certificates for `fisherman.teleport.computer`
  and `relay.fisherman.teleport.computer`.
- `.github/workflows/update-fisherman-dns.yml`, a manual workflow that
  points those Cloudflare DNS records at the current EC2 IPv4 address.

The old Phala/CVM CI/CD path has been removed. Do not recreate
`deploy-cvm.yml`, `bootstrap-cvm.yml`, or attestation-monitor automation
for Fisherman Cloud unless the product intentionally moves back to a TEE
architecture.

## Health Checks

Use the Cloud health command before switching clients:

```bash
fisherman cloud audit https://fisherman.teleport.computer
curl -fsS https://fisherman.teleport.computer/health
curl -fsS https://relay.fisherman.teleport.computer/health
```

`fisherman cloud audit` reads `/health` and verifies that the endpoint is
reachable and ingest-ready. It no longer verifies TEE attestation.

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
PRs. Treat AWS, Cloudflare, database, R2, and OpenRouter/OpenAI keys as
deployment-control or data-plane credentials.

## Client Setup

Clients should use:

```bash
fisherman backend configure cloud
```

If the account is not enabled yet, the daemon keeps capture local and
queues uploads until Cloud account setup is complete.
