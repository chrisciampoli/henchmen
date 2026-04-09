# Changelog

All notable changes will be documented in this file.

## [0.1.0] - 2026-04-08

### Added
- Initial open source release
- Provider interface layer with 6 abstractions (MessageBroker, DocumentStore, ObjectStore, ContainerOrchestrator, LLMProvider, CIProvider)
- GCP providers (Pub/Sub, Firestore, GCS, Cloud Run, Vertex AI Gemini, Cloud Build)
- AWS providers (SNS, DynamoDB, S3, ECS Fargate, Bedrock, CodeBuild)
- Local providers (in-memory, SQLite, filesystem, Docker, Ollama, shell CI)
- OpenAI and Anthropic direct API LLM providers
- Docker Compose local development stack with Ollama
- `henchmen serve` single-process CLI command
- 7 villain-themed components: Mastermind, Dispatch, Operative, Arsenal, Forge, Dossier, Schemes
- Apache 2.0 license
