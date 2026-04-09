# networking

Provisions the Henchmen VPC, primary subnet, egress-allowlist firewall rules, and the Serverless VPC Access Connector used by Cloud Run services to reach private Google APIs. The network is designed for default-deny egress with an explicit allowlist for GitHub, Slack, Jira/Atlassian, and Google APIs, so Operatives cannot exfiltrate data to arbitrary destinations.

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
| labels | map(string) | `{}` | Labels to apply to networking resources. |
| allowlist_cidrs | list(string) | `[]` | Additional egress CIDR ranges to allow (e.g. Slack, Jira/Atlassian). |

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
- `google_compute_firewall.deny_all_egress` — Default-deny egress rule.
- `google_compute_firewall.allow_egress_allowlist` — Explicit egress allowlist for GitHub, Google APIs, Slack, and Jira/Atlassian.
- `google_vpc_access_connector.connector` — Serverless VPC connector for Cloud Run.
