# EKS version upgrade

> **Owner:** Platform · **Last updated:** 2026-04-02

## tl;dr

1. Read the k8s release notes for the target version
2. Run `kube-no-trouble` against the current cluster to catch deprecated APIs
3. Apply the Terraform change to the control plane (online, ~30 min)
4. Rolling update the node groups one at a time
5. Smoke test with `platform-smoke` helm chart
6. If anything fails: stop the rollout and **roll forward**; EKS control planes cannot be downgraded

## Pre-flight

```bash
# Check deprecated APIs
kubectl krew install no-trouble
kubectl no-trouble --context=prod

# Read upstream notes
open https://kubernetes.io/docs/setup/release/notes/
```

## Control plane

```bash
cd acme-infra/terraform/prod/eks
# edit cluster_version in eks.tf
terraform plan
terraform apply
```

The control plane update is online but takes ~30 min. No pod disruption.

## Node groups

Do one node group at a time. Apply Terraform, which triggers a rolling launch template update. Nodes drain and replace one at a time.

```bash
# Monitor
kubectl get nodes -w
```

## Smoke test

```bash
helm install platform-smoke ./charts/platform-smoke -n prod-smoke
kubectl -n prod-smoke wait --for=condition=ready pod -l app=platform-smoke --timeout=5m
helm uninstall platform-smoke -n prod-smoke
```

## Rollback

EKS does not support control plane downgrades. If the control plane upgrade fails, you are stuck on the new version. Roll forward to the fix.

Node group rollbacks: revert the launch template Terraform change, apply, nodes roll back.

## Related

- `deploys/deploy-rollback.md`
- `oncall/handoff.md` — remember to flag an in-progress upgrade in the handoff note
