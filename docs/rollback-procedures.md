# Rollback Procedures

When something goes wrong on a self-hosted Henchmen stack, you have
three rollback layers available on GCP: container image pin (fastest,
~30s), Cloud Run revision traffic shift (~15s), and Terraform state
revert (slowest, ~5 min). This doc walks through each.

If you followed [`deploy-gcp.md`](deploy-gcp.md) to provision the
stack, the commands below use the same `PROJECT_ID` / `REGION`
variables.

If you're running Henchmen in local mode (docker-compose or
`henchmen serve`), GCP-specific rollback steps don't apply — instead:

| GCP procedure          | Local-mode equivalent                               |
|------------------------|-----------------------------------------------------|
| Container image pin    | `git checkout <sha>`, then `henchmen build-operative` and restart `henchmen serve` (or `docker compose up --build`) |
| Cloud Run revision     | restart the process / container                    |
| Terraform revert       | not applicable — no infra state                    |
| Emergency stop         | `docker compose down` (or kill `henchmen serve`); stop operatives with `docker ps` / `docker stop` |
| Drain the task queue   | restart the process — the in-memory broker keeps no messages across restarts; escalate unfinished tasks in `task_executions` (see `incident-runbook.md`) |

See [`incident-runbook.md`](incident-runbook.md) for the full
incident-response flow (triage, communication, postmortem).

## Container Rollback

All container images are stored in Artifact Registry at `us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/`.

### Revert to Previous Image Tag

1. List recent image digests:
   ```bash
   gcloud artifacts docker images list \
     us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/{service} \
     --include-tags --sort-by=~CREATE_TIME --limit=5
   ```

2. Identify the previous working digest or tag.

3. For an immediate rollback of one service, point it at the previous image:
   ```bash
   gcloud run services update henchmen-dev-{service} \
     --project=${PROJECT_ID} \
     --region=us-central1 \
     --image=us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/{service}@sha256:{digest}
   ```

   Terraform owns the image, so the next `terraform apply` restores
   `container_image_tag`. Make the rollback stick with step 4.

4. Pin the previous release for every service and the operative. Mastermind
   launches lairs from `operative:<HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG>`, which
   Terraform sets from the same `container_image_tag`, so tag the known-good
   images and apply:
   ```bash
   for svc in dispatch mastermind forge operative; do
     gcloud artifacts docker tags add \
       us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}@sha256:{digest_for_svc} \
       us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:rollback-{date}
   done
   # terraform.tfvars: container_image_tag = "rollback-{date}"
   cd terraform/environments/dev && terraform apply
   ```
   Lairs created after the apply run the rolled-back operative. The
   `henchmen-dev-lair-template` job is only a reference copy; updating it does
   not change what operatives run.

### Services and Their Images

| Service | Cloud Run Name | Image Path |
|---------|---------------|------------|
| Dispatch | `henchmen-dev-dispatch` | `.../henchmen-dev/dispatch:<container_image_tag>` |
| Mastermind | `henchmen-dev-mastermind` | `.../henchmen-dev/mastermind:<container_image_tag>` |
| Forge | `henchmen-dev-forge` | `.../henchmen-dev/forge:<container_image_tag>` |
| Operative | `lair-<task>-<node>-<suffix>` (one job per agentic node) | `.../henchmen-dev/operative:<container_image_tag>` |

## Cloud Run Revision Rollback

Cloud Run maintains a history of deployed revisions. To roll back to a previous revision:

1. List revisions:
   ```bash
   gcloud run revisions list \
     --service=henchmen-dev-{service} \
     --project=${PROJECT_ID} \
     --region=us-central1
   ```

2. Route 100% traffic to the previous revision:
   ```bash
   gcloud run services update-traffic henchmen-dev-{service} \
     --project=${PROJECT_ID} \
     --region=us-central1 \
     --to-revisions={previous-revision-name}=100
   ```

   Terraform does not pin traffic, so the next `terraform apply` that creates a
   new revision sends traffic to it again. Pin `container_image_tag` (see above)
   before applying.

3. Verify the service is healthy:
   ```bash
   curl -s https://henchmen-dev-{service}-{hash}.run.app/health
   ```

## Terraform Rollback

### State Management

Terraform state is stored in GCS under the `terraform/state` prefix of the bucket passed to `terraform init -backend-config=bucket=...` (by convention `henchmen-tfstate-<project_id>-<env>`; see `terraform/environments/dev/backend.tf`). If versioning is enabled on the bucket (`gcloud storage buckets update gs://<bucket> --versioning`), you can recover an earlier state object if one is corrupted.

**WARNING:** Never manually edit Terraform state. Use `terraform state` commands.

### Rollback a Terraform Change

1. Check the git log for the last known-good Terraform commit:
   ```bash
   git log --oneline terraform/
   ```

2. Revert the Terraform files to the previous version:
   ```bash
   git checkout {good_commit} -- terraform/
   ```

3. Plan and verify:
   ```bash
   cd terraform/environments/dev
   terraform plan -out=rollback.plan
   ```

4. Review the plan carefully -- ensure it only reverts the intended changes.

5. Apply:
   ```bash
   terraform apply rollback.plan
   ```

6. Terraform owns every Cloud Run environment variable and secret mount
   (`GITHUB_TOKEN` from `henchmen-dev-github-token`, `SLACK_BOT_TOKEN` from
   `henchmen-dev-slack-bot-token`, ...), so the apply restores the mounts the
   reverted configuration declares. Anything added by hand with
   `gcloud run services update --set-env-vars/--set-secrets` is removed; if it
   is still needed, add it to the `cloud-run-services` module rather than
   re-adding it with gcloud. Verify:
   ```bash
   gcloud run services describe henchmen-dev-{service} \
     --project=${PROJECT_ID} --region=us-central1 \
     --format="yaml(spec.template.spec.containers[0].env)"
   ```

### Terraform State Lock

If a Terraform operation was interrupted and the state is locked:

```bash
# Check lock info
terraform force-unlock {lock_id}
```

Use `force-unlock` only when you are certain no other operation is running.

## Emergency Procedures

### Disable All Pub/Sub Triggers

To stop all message processing (emergency brake):

```bash
# Pause all push subscriptions by removing their push endpoints
for sub in $(gcloud pubsub subscriptions list --project=${PROJECT_ID} --format="value(name)" | grep henchmen-dev); do
  gcloud pubsub subscriptions modify-push-config "$sub" --push-endpoint="" --project=${PROJECT_ID}
done
```

To re-enable:
```bash
# Re-apply push configs from Terraform. The pubsub module is nested inside
# the environment's `henchmen` module.
cd terraform/environments/dev
terraform apply -target=module.henchmen.module.pubsub
```

### Drain the Task Queue

To acknowledge and discard all pending messages on a topic:

```bash
# Pull and auto-ack messages (drains the subscription)
while gcloud pubsub subscriptions pull henchmen-dev-task-intake-sub \
  --project=${PROJECT_ID} --limit=100 --auto-ack 2>/dev/null | grep -q "DATA"; do
  echo "Draining..."
done
echo "Queue drained."
```

### Stop All Running Operatives

To cancel all in-progress Cloud Run Job executions:

```bash
# Every operative is its own lair-* job, so list executions across all jobs.
for exec_id in $(gcloud run jobs executions list \
  --project=${PROJECT_ID} \
  --region=us-central1 \
  --filter="metadata.name ~ ^lair- AND status.completionTime:null" \
  --format="value(metadata.name)"); do
  gcloud run jobs executions cancel "$exec_id" --project=${PROJECT_ID} --region=us-central1 --quiet
done
```

### Full System Shutdown

In case of a security incident or runaway cost:

1. **Stop Dispatch** (prevents new tasks):
   ```bash
   gcloud run services update henchmen-dev-dispatch --project=${PROJECT_ID} --region=us-central1 --no-traffic
   ```

2. **Disable Pub/Sub** (stops message flow):
   ```bash
   # See "Disable All Pub/Sub Triggers" above
   ```

3. **Cancel running operatives**:
   ```bash
   # See "Stop All Running Operatives" above
   ```

4. **Stop Mastermind and Forge**:
   ```bash
   gcloud run services update henchmen-dev-mastermind --project=${PROJECT_ID} --region=us-central1 --no-traffic
   gcloud run services update henchmen-dev-forge --project=${PROJECT_ID} --region=us-central1 --no-traffic
   ```

### Restart After Shutdown

1. Re-enable traffic on services (reverse order):
   ```bash
   gcloud run services update-traffic henchmen-dev-forge --project=${PROJECT_ID} --region=us-central1 --to-latest
   gcloud run services update-traffic henchmen-dev-mastermind --project=${PROJECT_ID} --region=us-central1 --to-latest
   gcloud run services update-traffic henchmen-dev-dispatch --project=${PROJECT_ID} --region=us-central1 --to-latest
   ```

2. Re-enable Pub/Sub push subscriptions (via Terraform or manual push endpoint config).

3. Verify each service health endpoint returns 200.

4. Monitor dead letter queue for any backed-up messages that need reprocessing.
