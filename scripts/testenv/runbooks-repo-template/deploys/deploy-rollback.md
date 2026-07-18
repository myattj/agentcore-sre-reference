# Deploy rollback

> **Owner:** SRE · **Last updated:** 2026-03-30

## tl;dr

Two options:

1. **GitHub Actions `rollback-deploy` workflow** (preferred) — re-tags the previous image and triggers a real deploy. Takes ~3 min. Updates the git state so next deploy is from the rolled-back commit.
2. **`kubectl rollout undo`** (emergency only) — takes ~30 sec but does NOT update the git state. Use for "oh shit" moments, then immediately trigger option 1 to realign git.

## Decision tree

```
Is this bad enough to rollback?
├── "Yes, and I have 5 min" → Option 1
├── "Yes, and it's bleeding right now" → Option 2, then Option 1
└── "Not sure" → Use the debugger first, not the rollback button
```

## Option 1 — Actions workflow

```bash
gh workflow run rollback-deploy.yml \
  --ref main \
  -f service=checkout-api \
  -f target_sha=PREVIOUS_SHA
```

Monitor in the Actions tab. The workflow:

1. Re-tags the previous image as the new "production" tag
2. Triggers the standard deploy job
3. Posts to `#eng-general` when done

## Option 2 — kubectl emergency rollback

```bash
kubectl -n prod rollout undo deployment/checkout-api
kubectl -n prod rollout status deployment/checkout-api
```

**Immediately after**, trigger Option 1 so the git state matches what's actually deployed. Otherwise the next deploy will re-deploy the broken version.

## Services this applies to

All services deployed from `acme-data-api`:

- `checkout-api`
- `orders-api`
- `user-service`
- `reporting-worker`
- `ingest-pipeline`

## Related

- `incidents/kickoff.md` — if the rollback is part of an incident
- `oncall/handoff.md`
