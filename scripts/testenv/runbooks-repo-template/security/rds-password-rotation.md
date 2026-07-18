# RDS password rotation (prod)

> **Owner:** SRE · **Last updated:** 2026-03-28

## tl;dr

1. Create a new password in AWS Secrets Manager as a new version stage on the existing secret
2. Trigger a `checkout-api` rolling deploy so new pods pick up the new secret
3. Canary a request against `/healthz`
4. Promote the new version to `AWSCURRENT`
5. **Wait 24h**
6. Delete the old version

## Why the wait?

Some long-lived connections (especially the `reporting-worker` pool) can hold the old credential up to 12 hours after a rotation. Deleting the old version inside 24h has broken production twice. Just wait.

## Steps

### 1. Generate + store the new password

```bash
umask 077
RDS_SECRET_FILE=$(mktemp)
trap 'rm -f "$RDS_SECRET_FILE"' EXIT
python3 - <<'PY' >"$RDS_SECRET_FILE"
import json, secrets
print(json.dumps({"password": secrets.token_urlsafe(32)}))
PY
aws secretsmanager put-secret-value \
  --secret-id agentcore/services/rds-prod \
  --secret-string "file://$RDS_SECRET_FILE" \
  --version-stages AWSPENDING
```

### 2. Rolling deploy `checkout-api`

```bash
kubectl -n prod rollout restart deployment/checkout-api
kubectl -n prod rollout status deployment/checkout-api --timeout=5m
```

### 3. Canary

```bash
curl -sS https://checkout.internal/healthz | jq .
```

The healthz endpoint returns `{"db": "ok"}` only if the new secret is actually in use.

### 4. Promote

```bash
aws secretsmanager update-secret-version-stage \
  --secret-id agentcore/services/rds-prod \
  --version-stage AWSCURRENT \
  --move-to-version-id "$NEW_VERSION_ID"
```

### 5. Wait 24h

Seriously. See top of file.

### 6. Delete old version

```bash
aws secretsmanager update-secret-version-stage \
  --secret-id agentcore/services/rds-prod \
  --remove-from-version-id "$OLD_VERSION_ID" \
  --version-stage AWSPREVIOUS
```

## Related

- `deploys/deploy-rollback.md` — if the rolling deploy fails
- `infra/rds-connection-exhaustion.md` — debugging connection errors after rotation
