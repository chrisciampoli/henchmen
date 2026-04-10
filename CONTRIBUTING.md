# Contributing to Henchmen

Thank you for your interest in contributing. This guide covers everything you need to get started.

## Getting Started

1. Fork the repository and clone your fork:
   ```bash
   git clone https://github.com/your-username/henchmen.git
   cd henchmen
   ```

2. Install with dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

3. (Optional) Install pre-commit hooks for secret scanning and auto-formatting:
   ```bash
   pip install pre-commit
   pre-commit install
   ```

4. Verify the setup:
   ```bash
   pytest tests/unit/ -q
   ```

## Development

Run the full checklist before submitting any change:

```bash
ruff check --fix src/ tests/   # Auto-fix lint
ruff check src/ tests/          # Verify clean
ruff format src/ tests/         # Format
mypy src/                       # Type check
pytest tests/unit/              # Unit tests
```

All five must pass. No exceptions.

## Code Style

- Python 3.12+ with modern typing (`str | None`, not `Optional[str]`)
- Pydantic v2 with `Field(...)` descriptors for all models
- `pydantic-settings` with `HENCHMEN_` env prefix and `@lru_cache` singletons
- Ruff for linting and formatting (120 character line length, rules: E, F, I, N, W, UP)
- mypy strict mode — all code must type-check cleanly
- `datetime.now(timezone.utc)` for all timestamps — no naive datetimes
- `str(uuid4())` for IDs
- `str, Enum` pattern for string enums
- Module-level docstrings on all files
- snake_case for variables and functions, PascalCase for classes
- Pydantic models for all data crossing component boundaries — never raw dicts

## Async Test Conventions

Henchmen uses **strict** `asyncio_mode` in pytest (`pyproject.toml`). This means:

- Every async test function **must** carry an explicit `@pytest.mark.asyncio` decorator.
- Every async fixture **must** be declared with `@pytest_asyncio.fixture` instead of `@pytest.fixture`.
- Settings isolation between tests is handled by the `_isolate_settings`
  autouse fixture in `tests/conftest.py` — do **not** call
  `get_settings.cache_clear()` manually in individual tests.
- Shared fixtures such as `mock_settings` (in `tests/conftest.py`) and
  `dispatch_client` (in `tests/integration/conftest.py`) should be preferred
  over hand-rolled helpers or re-instantiating ASGI clients per test.

Rationale: "explicit over implicit" keeps the asyncio mode load-bearing, so a
future mode flip cannot silently skip async tests.

## Adding a Provider

Providers live in `src/henchmen/providers/`. Each provider implements a set of abstract interfaces defined in `src/henchmen/providers/base.py`.

To add a new cloud or infrastructure provider:

1. Create `src/henchmen/providers/yourprovider/` with an `__init__.py`
2. Implement all 6 interfaces:
   - `MessageBroker` — Pub/Sub or equivalent
   - `DocumentStore` — Firestore or equivalent
   - `ObjectStore` — GCS/S3 or equivalent
   - `ContainerOrchestrator` — Cloud Run Jobs or equivalent
   - `LLMProvider` — Vertex AI, Bedrock, or direct API
   - `CIProvider` — Cloud Build, CodeBuild, or shell equivalent
3. Register your provider in `src/henchmen/providers/registry.py`
4. Add the required SDK dependencies to `pyproject.toml` as a new optional group
5. Write unit tests in `tests/unit/test_yourprovider_providers.py`

## Adding an LLM Provider

LLM providers implement the `LLMProvider` interface:

```python
from henchmen.providers.interfaces import LLMProvider
from henchmen.models.llm import LLMResponse, Message, ToolDefinition

class MyLLMProvider:
    async def generate(self, messages: list[Message], model: str,
                       tools: list[ToolDefinition] | None = None, ...) -> LLMResponse:
        ...

    async def count_tokens(self, text: str, model: str) -> int:
        ...

    def supported_models(self) -> list[str]:
        ...

    def resolve_tier(self, tier: str) -> str:
        ...
```

Register it in `src/henchmen/providers/registry.py` and add the SDK to a new optional group in `pyproject.toml`.

## Pull Requests

- Branch from `main` with a descriptive name: `feat/my-feature` or `fix/the-bug`
- Write unit tests for all new behavior
- Keep PRs focused — one logical change per PR
- All CI checks must pass before review
- Write clear commit messages explaining why, not just what

## Git Authorship — HARD RULE

**All commits must be authored by the human contributor.**

Never attribute commits to AI tools. No `Co-Authored-By` lines crediting AI assistants. If you used an AI tool to help write code, the commit author is still you — the human who reviewed, tested, and chose to submit it.

## Questions

Open a GitHub Discussion for design questions. Open an issue for bugs or feature requests.
