# networking

Provisions the Henchmen VPC, its primary subnet, an allow-internal ingress firewall rule, and the Serverless VPC Access Connector Cloud Run uses to reach private-range resources.

This module does **not** restrict egress. Every service and the lair template set `egress = "PRIVATE_RANGES_ONLY"`, so public traffic (GitHub, Slack, Jira, Google APIs) never traverses the VPC, and the lair jobs Mastermind creates at runtime attach no connector at all. Real egress control would need Cloud NAT, an egress allowlist or HTTPS proxy, and `ALL_TRAFFIC` egress with the connector attached to runtime-created jobs; see the header comment in `main.tf`.

## Usage

```hcl
module "networking" {
  source      = "../../modules/networking"
  project_id  = var.project_id
  region      = var.region
  environment = var.environment
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region to deploy networking resources into. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| subnet_cidr | string | `10.0.0.0/20` | The IP CIDR range for the primary subnet. |

## Outputs

| Name | Description |
|---|---|
| vpc_id | The ID of the Henchmen VPC network. |
| vpc_name | The name of the Henchmen VPC network. |
| subnet_id | The ID of the Henchmen primary subnet. |
| subnet_name | The name of the Henchmen primary subnet. |
| connector_id | The ID of the VPC Serverless Access Connector. |
| connector_name | The name of the VPC Serverless Access Connector. |

## Resources created

- `google_compute_network.vpc` — The Henchmen VPC (no auto subnets).
- `google_compute_subnetwork.subnet` — Primary regional subnet with private Google access.
- `google_compute_firewall.allow_internal` — Allow-all ingress within the subnet CIDR.
- `google_vpc_access_connector.connector` — Serverless VPC connector (`10.8.0.0/28`, e2-micro, 2–3 instances).

VPC networks, subnets and firewall rules do not support labels, so this module takes none.
