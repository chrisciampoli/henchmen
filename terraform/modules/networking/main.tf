# ---------------------------------------------------------------------------
# Networking
#
# The VPC exists so Cloud Run can reach private-range resources through a
# Serverless VPC Access connector. Public egress (GitHub, Slack, Jira, Google
# APIs) deliberately does NOT traverse this VPC: every service and lair sets
# `vpc_access { egress = "PRIVATE_RANGES_ONLY" }`, and the jobs Mastermind
# creates at runtime attach no connector at all.
#
# There is therefore no egress firewall here. A previous revision carried a
# deny-all egress rule plus a hand-maintained CIDR allowlist for GitHub /
# Slack / Atlassian; because no Henchmen traffic ever traversed the VPC, those
# rules governed nothing while reading as if outbound traffic were controlled.
#
# Making egress control real needs all three of: Cloud NAT on this subnet, an
# egress allowlist (or, better, an HTTPS proxy that resolves names at request
# time instead of pinning third-party IP ranges), and `egress = "ALL_TRAFFIC"`
# with the connector attached to the runtime-created lair jobs — which is a
# code change in the Cloud Run orchestrator, not a terraform one. Until all
# three land, this module does not claim to restrict egress.
# ---------------------------------------------------------------------------

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "henchmen-${var.environment}-vpc"
  auto_create_subnetworks = false
  description             = "Henchmen Agent Factory VPC network"
}

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

# VPC Serverless Connector (for Cloud Run to reach the VPC).
# min_instances = 2 is the floor the API enforces for a connector.
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
