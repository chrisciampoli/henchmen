# VPC Network
resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "henchmen-${var.environment}-vpc"
  auto_create_subnetworks = false
  description             = "Henchmen Agent Factory VPC network"
}

# Subnet
resource "google_compute_subnetwork" "subnet" {
  project                  = var.project_id
  name                     = "henchmen-${var.environment}-subnet"
  ip_cidr_range            = var.subnet_cidr
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true
  description              = "Henchmen Agent Factory primary subnet"
}

# Firewall: allow all internal traffic within the subnet
resource "google_compute_firewall" "allow_internal" {
  project     = var.project_id
  name        = "henchmen-${var.environment}-allow-internal"
  network     = google_compute_network.vpc.id
  description = "Allow all internal traffic within the henchmen subnet"
  direction   = "INGRESS"
  priority    = 1000

  allow {
    protocol = "all"
  }

  source_ranges = [var.subnet_cidr]
}

# Firewall: deny all egress (default deny)
resource "google_compute_firewall" "deny_all_egress" {
  project     = var.project_id
  name        = "henchmen-${var.environment}-deny-all-egress"
  network     = google_compute_network.vpc.id
  description = "Deny all egress traffic by default"
  direction   = "EGRESS"
  priority    = 1000

  deny {
    protocol = "all"
  }

  destination_ranges = ["0.0.0.0/0"]
}

# Firewall: allow egress to permitted external endpoints
# GitHub API: 140.82.112.0/20, 192.30.252.0/22
# Google APIs / Vertex AI: 199.36.153.4/30 (restricted.googleapis.com), 34.126.0.0/18
# Slack API: 54.84.0.0/13, 52.20.0.0/14 (approximate; override via allowlist_cidrs)
# Atlassian/Jira: 104.192.136.0/21
resource "google_compute_firewall" "allow_egress_allowlist" {
  project     = var.project_id
  name        = "henchmen-${var.environment}-allow-egress-allowlist"
  network     = google_compute_network.vpc.id
  description = "Allow egress to GitHub API, Vertex AI, Google APIs, Slack, and Atlassian/Jira"
  direction   = "EGRESS"
  priority    = 900

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }

  destination_ranges = concat(
    [
      # GitHub API
      "140.82.112.0/20",
      "192.30.252.0/22",
      # Google APIs (restricted.googleapis.com)
      "199.36.153.4/30",
      # Vertex AI / Google Cloud APIs
      "34.126.0.0/18",
      # Slack API
      "54.80.0.0/13",
      "52.20.0.0/14",
      # Atlassian / Jira
      "104.192.136.0/21",
    ],
    var.allowlist_cidrs,
  )
}

# VPC Serverless Connector (for Cloud Run to reach the VPC)
resource "google_vpc_access_connector" "connector" {
  project       = var.project_id
  name          = "henchmen-${var.environment}-connector"
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = "10.8.0.0/28"
  machine_type  = "e2-micro"
  min_instances = 2
  max_instances = 3
}
