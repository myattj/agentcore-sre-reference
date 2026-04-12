# IAM key rotation

> **Owner:** Security · **Last updated:** 2026-02-28

## tl;dr

Rotate keys for service accounts (NOT human users — those use SSO). Sequence:

1. Create new key
2. Deploy service with new key
3. Verify new key works
4. Disable old key (don't delete yet)
5. **Wait 24 hours**
6. Delete old key

Do NOT skip the 24h wait. We've tripped CloudTrail alarms twice doing that — the key is cached in places you don't know about.

## Step 1 — Create new key

```bash
new_key=$(aws iam create-access-key --user-name deploy-svc)
echo $new_key | jq -r '.AccessKey | "ID=" + .AccessKeyId + "\nSECRET=" + .SecretAccessKey'
```

Store the credentials in AWS Secrets Manager at `agentcore/services/<service>-iam`:

```bash
aws secretsmanager put-secret-value \
  --secret-id agentcore/services/<service>-iam \
  --secret-string "{\"access_key_id\":\"$ID\",\"secret_access_key\":\"$SECRET\"}"
```

## Step 2 — Deploy service with new key

The service reads the secret on startup. Trigger a rolling restart:

```bash
kubectl -n prod rollout restart deployment/<service>
kubectl -n prod rollout status deployment/<service>
```

## Step 3 — Verify

Tail CloudTrail for the new key ID being used. It should appear within 1 minute of the rollout completing:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=AccessKeyId,AttributeValue="$NEW_KEY_ID" \
  --max-results 5
```

If you see no events, something's wrong. Roll back and investigate.

## Step 4 — Disable (NOT delete) old key

```bash
aws iam update-access-key \
  --user-name deploy-svc \
  --access-key-id "$OLD_KEY_ID" \
  --status Inactive
```

Disabling is reversible. Deleting is not.

## Step 5 — Wait 24 hours

Seriously. Set a calendar reminder. Go work on something else.

During the wait, monitor CloudTrail for any use of the disabled key. If anything tries to use it, you have an environment that still has it cached somewhere — re-enable, deploy the fix, then rotate again.

## Step 6 — Delete

```bash
aws iam delete-access-key \
  --user-name deploy-svc \
  --access-key-id "$OLD_KEY_ID"
```

## What about human IAM users?

We don't have any. All human access is via AWS SSO federated from Okta. If you think you need to create an IAM user for a human, you don't — you need to add them to an Okta group.

## Related

- `security/rds-password-rotation.md`
- `security/secret-leaked-in-logs.md`
