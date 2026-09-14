# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| < 0.2   | No        |

Henchmen is pre-1.0. Security fixes are released against the latest 0.2.x
minor line. Users on older 0.x lines should upgrade.

## Threat Model

Henchmen is an AI agent factory that receives untrusted input from humans
and third-party systems, runs LLM-driven code changes, and pushes pull
requests to a target repository. Our working assumptions are:

**Untrusted**

- Task payloads from Slack, GitHub, Jira, and HTTP clients (attackers can
  control title, description, and labels).
- Content of the target repository at the time of clone (an attacker could
  have landed a poisoned file via a previous PR).
- Webhook payloads (must be signature-verified before any processing).
- Outputs from LLMs (treated as data, never as trusted commands).

**Trusted**

- Operators with access to `.env.local`, Secret Manager, or the deploying
  service account.
- The Secret Manager backend itself (GCP Secret Manager, AWS Secrets
  Manager, or the equivalent on other clouds).
- The container runtime the operative executes in (ephemeral Cloud Run Job
  or Docker container with a clean workspace).

**In scope**

- Authentication bypass on webhook and API endpoints.
- Privilege escalation from a task payload to operator-level access.
- Secret leakage through logs, metrics, PR descriptions, or error responses.
- Sandbox escapes from the operative container to the host.
- Fail-open behaviour that lets a failing CI check ship a PR.
- Cost exhaustion attacks via crafted payloads that run the operative loop
  indefinitely.

**Out of scope**

- Issues that require control of the operator's laptop, Secret Manager,
  GitHub token, or cloud provider account.
- LLM hallucinations or model quality issues that do not cross a security
  boundary.
- Attacks that require a malicious custom Scheme committed to the repo by
  a trusted maintainer.
- Denial of service against Ollama or third-party LLM APIs.

## Local Deployments and the Docker Socket

The local image (`docker build --target local`, published as
`ghcr.io/<owner>/henchmen/local`) and the Docker Compose stack both mount the
host's Docker socket (`/var/run/docker.sock`) and run as root inside the
container, so that Henchmen can launch operative containers. Access to that
socket is equivalent to control of the local Docker engine, and through it
effectively root on the host: anything that compromises the Henchmen
container can start, stop or inspect any container on the machine.

- Run the local image or Compose stack only on a machine you control, not on
  a shared or multi-tenant host.
- Keep the port on loopback. The documented `docker run` command publishes
  the Console and the services only on `127.0.0.1:8000`, and the Console
  itself refuses requests whose `Host` is not `127.0.0.1`, `localhost` or
  `[::1]`. The server listens on `0.0.0.0` *inside* the container, so it is
  the `-p 127.0.0.1:8000:8000` mapping that keeps it off the network; the
  bundled `docker-compose.yml` publishes `8000:8000` on every interface, so
  change it to `127.0.0.1:8000:8000` on any machine reachable by others.

## Reporting a Vulnerability

Please use GitHub Security Advisories:
https://github.com/chrisciampoli/henchmen/security/advisories/new

If that is not available, email chrisciampoli@gmail.com with the subject
line `SECURITY: <short description>`. PGP key fingerprint placeholder --
open an issue titled "request PGP key" if you need end-to-end encryption
and we will publish one.

**Do not open public GitHub issues for security vulnerabilities.**

## Safe Harbour

We support good-faith security research. If you:

- Make a good-faith effort to avoid privacy violations, data destruction,
  and service interruption,
- Only interact with accounts and data you own or have explicit permission
  to access,
- Give us a reasonable window to investigate and fix before disclosure,

then we will not pursue or support legal action against you, and we will
work with you on coordinated disclosure.

## Response Targets (best effort)

- Acknowledgement: within 3 business days.
- Initial assessment: within 7 business days.
- Fix or documented mitigation: within 30 days for High/Critical, 90 days
  for Medium, best-effort for Low.

These are targets, not guarantees. Henchmen is maintained by a small team;
please be patient if you do not hear back on day one.
