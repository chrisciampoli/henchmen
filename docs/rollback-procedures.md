# Rollback Procedures -- Henchmen

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

3. Update the Cloud Run service to the previous image:
   ```bash
   gcloud run services update henchmen-dev-{service} \
     --project=${PROJECT_ID} \
     --region=us-central1 \
     --image=us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/{service}@sha256:{digest}
   ```

4. For the Operative, also update the lair template:
   ```bash
   gcloud run jobs update henchmen-dev-lair-template \
     --project=${PROJECT_ID} \
     --region=us-central1 \
     --image=us-central1-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/operative@sha256:{digest}
   ```

### Services and Their Images

| Service | Cloud Run Name | Image Path |
|---------|---------------|------------|
| Dispatch | `henchmen-dev-dispatch` | `.../henchmen-dev/dispatch:latest` |
| Mastermind | `henchmen-dev-mastermind` | `.../henchmen-dev/mastermind:latest` |
| Forge | `henchmen-dev-forge` | `.../henchmen-dev/forge:latest` |
| Operative | `henchmen-dev-lair-template` (job) | `.../henchmen-dev/operative:latest` |

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

3. Verify the service is healthy:
   ```bash
   curl -s https://henchmen-dev-{service}-{hash}.run.app/health
   ```

## Terraform Rollback

### State Management

Terraform state is stored in GCS: `gs://<YOUR_TFSTATE_BUCKET>/` (configured in `backend.tf`).

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

6. **CRITICAL:** After `terraform apply`, re-set any secret environment variables that Terraform resets:
   ```bash
   # Terraform apply strips manually-set env vars from Cloud Run services.
   # Re-apply secrets for each affected service:
   gcloud run services update henchmen-dev-{service} \
     --project=${PROJECT_ID} \
     --region=us-central1 \
     --set-secrets=GITHUB_APP_PRIVATE_KEY=github-app-private-key:latest,SLACK_BOT_TOKEN=slack-bot-token:latest
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
# Re-apply push configs from Terraform
cd terraform/environments/dev
terraform apply -target=module.pubsub
# Then re-set secrets (see above)
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
for exec_id in $(gcloud run jobs executions list \
  --job=henchmen-dev-lair-template \
  --project=${PROJECT_ID} \
  --region=us-central1 \
  --filter="status.conditions.type=Completed AND status.conditions.status!=True" \
  --format="value(name)"); do
  gcloud run jobs executions cancel "$exec_id" --project=${PROJECT_ID} --region=us-central1
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
