output "topic_ids" {
  description = "Map of logical topic name to Pub/Sub topic ID"
  value = {
    task_intake         = google_pubsub_topic.task_intake.id
    task_planned        = google_pubsub_topic.task_planned.id
    operative_dispatch  = google_pubsub_topic.operative_dispatch.id
    operative_status    = google_pubsub_topic.operative_status.id
    operative_complete  = google_pubsub_topic.operative_complete.id
    forge_request       = google_pubsub_topic.forge_request.id
    forge_result        = google_pubsub_topic.forge_result.id
    dead_letter         = google_pubsub_topic.dead_letter.id
  }
}

output "subscription_ids" {
  description = "Map of logical subscription name to Pub/Sub subscription ID"
  value = {
    task_intake         = google_pubsub_subscription.task_intake.id
    task_planned        = google_pubsub_subscription.task_planned.id
    operative_dispatch  = google_pubsub_subscription.operative_dispatch.id
    operative_status    = google_pubsub_subscription.operative_status.id
    operative_complete  = google_pubsub_subscription.operative_complete.id
    forge_request       = google_pubsub_subscription.forge_request.id
    forge_result        = google_pubsub_subscription.forge_result.id
    dead_letter         = google_pubsub_subscription.dead_letter.id
  }
}
