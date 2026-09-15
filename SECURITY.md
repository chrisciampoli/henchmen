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

## Desktop Install Hardening

A data-directory install (`HENCHMEN_DATA_DIR`, the local image) is treated as a real installation:

- **No development fail-open paths.** Even with `HENCHMEN_ENVIRONMENT=dev`, unauthenticated Pub/Sub
  pushes, an open task API, unsigned webhooks, open `/metrics`, CI without a GitHub token, operatives
  without task state and simulated lair passes are all refused.
- **Host allowlist on the whole app.** Every route except `/health` refuses a `Host` other than
  `127.0.0.1`, `localhost`, `[::1]` or the operatives' container name (`HENCHMEN_LOCAL_CONTAINER_HOSTNAME`,
  default `henchmen`), which blocks DNS rebinding.
- **Internal authentication.** `<data dir>/secrets/` holds, owner-only: the Console session key, the
  one-time setup token, the internal push token (sent by the server's own broker on every simulated
  Pub/Sub push and required by the maintenance routes) and the operative task-token key. Each operative
  receives only an HMAC token for its own task, which authenticates its report and its three task-state
  calls (cost read, heartbeat, interrupted report) and nothing else. Operatives never mount the data volume.
- **One-time sign-in links.** The setup token is consumed on use and rotated at every start;
  `docker exec henchmen henchmen console-link` prints a fresh link and invalidates earlier ones.
  `HENCHMEN_CONSOLE_SETUP_TOKEN` only seeds the very first token a data directory ever issues, so a
  `console-link` run before the first `serve` uses that seed up. A launcher should therefore always get
  its link from `henchmen console-link` rather than rely on the seed.
- **Recovery without a crash loop.** A completed setup that cannot start serves the Console in
  needs-attention mode. The problem list is redacted and returned only to a signed-in Console session;
  an unauthenticated `/console/api/status` reports the mode with an empty problem list.
- **Operative-written code never runs in the server process.** On a desktop install (whenever the
  effective container orchestrator is local Docker) the Mastermind's lint and test gates, the `fix_lint`
  auto-fixer and Forge's PR lint and tests all run in a separate gate container from the operative image
  (`ci_gate`). Nothing is bind-mounted from the host, so the data volume and the Docker socket are never
  reachable from repository code. Forge keeps only its silent-failure scan in the server process: it
  clones without a checkout and scans the diff text, which executes nothing from the repository.
- **What the gate container guarantees about the GitHub token.** The token reaches the gate only on its
  standard input (`docker run -i`). It is never on a command line or in the container's or the docker
  CLI's environment. The gate process starts as root with every capability dropped except
  `CHOWN`/`DAC_OVERRIDE`/`FOWNER`/`SETUID`/`SETGID` and `no-new-privileges`. It runs every
  repository-controlled command (dependency installs, linters, test suites, fixers) as uid/gid 65534 with
  the workspace handed to that user, so that code cannot read the gate process's memory or
  `/proc/1/environ`. The gate process keeps the token in memory only, and only root-owned git uses it: the
  clone and the base-branch fetch, both before any repository code runs, and for `fix_lint` the push.
  Before repository code runs, `origin`'s URL in `.git/config` is reset to a token-less form. For
  `fix_lint`, the git directory is moved into a root-only directory first. Every unprivileged process is
  killed before the commit, and the commit and push run with hooks disabled against that private git
  directory. The token reaches the push only as an HTTP header in that one git process's environment.
  Not guaranteed: the token is still usable by the gate container's own root process and by git while it
  clones, fetches and pushes, and output from repository code is scrubbed of the token and known secret
  patterns but is otherwise shown in results and PR comments. A repository whose install step genuinely
  needs `GITHUB_TOKEN` (for example, GitHub Packages) fails its local gate — a deliberate fail-closed
  trade-off. The cloud CI path is unchanged.

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
