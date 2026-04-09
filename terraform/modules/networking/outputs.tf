output "vpc_id" {
  description = "The ID of the Henchmen VPC network"
  value       = google_compute_network.vpc.id
}

output "vpc_name" {
  description = "The name of the Henchmen VPC network"
  value       = google_compute_network.vpc.name
}

output "subnet_id" {
  description = "The ID of the Henchmen primary subnet"
  value       = google_compute_subnetwork.subnet.id
}

output "subnet_name" {
  description = "The name of the Henchmen primary subnet"
  value       = google_compute_subnetwork.subnet.name
}

output "connector_id" {
  description = "The ID of the VPC Serverless Access Connector"
  value       = google_vpc_access_connector.connector.id
}

output "connector_name" {
  description = "The name of the VPC Serverless Access Connector"
  value       = google_vpc_access_connector.connector.name
}
