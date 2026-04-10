# Henchmen Roadmap

> Living document — updated as priorities shift. Items are not commitments.

## Now (v0.2.x)

- **RAG provider abstraction** — decouple `dossier/embedder.py` from Vertex AI RAG Engine behind a generic interface (same pattern as LLM, MessageBroker, etc.)
- **AWS ECS launcher** — `ContainerOrchestrator` implementation for ECS Fargate so operatives can run outside GCP
- **Eval baselines** — deterministic eval suite (`evals/`) with pass-rate targets for bugfix and feature schemes

## Next (v0.3.x)

- **Multi-repo tasks** — allow a single task to span changes across multiple repositories
- **Plugin system for Arsenal tools** — let contributors register custom tools without modifying core Arsenal code
- **Grafana templates** — pre-built dashboards for operative latency, cost, and success rate

## Later (v1.0)

- **Stable public API** — freeze Task, Operative, and Scheme models with semver guarantees
- **PyPI publication** — `pip install henchmen` with extras for each cloud provider
- **Kubernetes operator** — CRD-based deployment as an alternative to Cloud Run
