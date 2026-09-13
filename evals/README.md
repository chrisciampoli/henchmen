# Henchmen Offline Evaluation Harness

The `evals/` tree holds Henchmen's BYO-LLM parity fixtures and baseline; the
runnable harness itself lives in the installed package at
`src/henchmen/evals/` (`evals/harness.py` and `evals/storage.py` are thin
import shims kept for repo checkouts). It answers the
question: *"If a contributor swaps the default Gemini provider out for
OpenAI, Anthropic, or Ollama, how much quality do they give up?"*

Unlike the production pipeline, the harness does **not** touch GCP,
Pub/Sub, Firestore, or Cloud Run. It runs entirely on the local filesystem
and talks directly to whichever `LLMProvider` you point it at. CI uses it
as a canary: a drop of more than 5% against the stored baseline fails the
job.

## How to run

```bash
# Run every fixture against OpenAI and print a summary.
henchmen eval --provider openai

# Run a single fixture (useful during development).
henchmen eval --provider openai --fixture bugfix_off_by_one

# Overwrite the baseline for the given provider with the current result.
henchmen eval --provider openai --write-baseline

# Compare the current run against the baseline; exits non-zero on regression.
henchmen eval --provider openai --compare-baseline
```

No GCP credentials are required: `henchmen eval` defaults `HENCHMEN_PROVIDER`
to `local` when you have not set it, so only the LLM provider needs
credentials.

Credentials come from `Settings`, which means the **`HENCHMEN_`-prefixed**
names only — `HENCHMEN_OPENAI_API_KEY`, `HENCHMEN_ANTHROPIC_API_KEY`,
`HENCHMEN_LLM_OLLAMA_BASE_URL`, `HENCHMEN_GCP_PROJECT_ID`. A bare
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` is *not* read (the operative container
is handed the `HENCHMEN_` ones, so an unprefixed key would work on your laptop
and fail in a real run). `henchmen doctor` reports the same way.

`--provider` takes the canonical registry names `openai`, `anthropic`, `gcp`,
`local` and `aws`; the friendly aliases `vertex`, `ollama` and `bedrock` are
accepted and normalised, so `--provider ollama` and `--provider local` write
the same `local` baseline entry.

Run history is stored in SQLite at `~/.henchmen/eval/results.db`; override the
location with `HENCHMEN_EVAL_DB_PATH`. History (`henchmen eval history` /
`compare`) needs the optional `aiosqlite` dependency: `pip install -e ".[dev]"`.

## How fixtures work

Each fixture is a self-contained test case under `evals/fixtures/<name>/`
with three pieces:

```
fixtures/<name>/
├── task.json                 # {title, description, scheme}
├── repo/                     # pristine starting state of a mini-repo
│   ├── ...source files...
│   └── pyproject.toml / package.json
└── expected/
    └── diff_patterns.json    # rubric the harness grades against
```

The harness copies `repo/` into a temp workspace, initialises a git repo
there, drives a minimal apply-patch agent loop against the configured
`LLMProvider`, and then compares `git diff HEAD` against
`expected/diff_patterns.json`. Scoring is **diff-based** so the LLM can't
cheat by writing a convincing summary while leaving the code broken.

### `diff_patterns.json` schema

```json
{
  "must_contain_file_change": ["list_utils.py"],
  "must_fix_tests": true,
  "expected_substrings_in_changed_code": ["items[-n:]"],
  "test_runner": "pytest",
  "test_command": ["pytest", "-q", "repo"]
}
```

| Field                                | Meaning                                                        |
|--------------------------------------|----------------------------------------------------------------|
| `must_contain_file_change`           | Every listed path must appear in `git diff --name-only HEAD`. |
| `must_fix_tests`                     | If `true`, the harness runs `test_command` and scores it.     |
| `expected_substrings_in_changed_code`| Every listed substring must appear in the diff.               |
| `test_runner`                        | Informational — one of `pytest`, `npm`, or `null`.            |
| `test_command`                       | Argv list executed in the workspace. `null` skips the runner. |
| `lint_command`                       | Optional argv list scored by the `conventions` dimension.     |

Scoring is fail-closed: if `test_command` is declared but the binary is
missing from `PATH` or the run times out, `tests_pass` is `false` (never
"not applicable"), and the reason is reported in `test_runner_error`. The
same applies to a declared `lint_command`, which scores `conventions` 0.0
when the linter cannot run.

### Scoring weights

| Signal                          | Weight |
|---------------------------------|--------|
| `diff_non_empty`                | 25%    |
| `touched_expected_files`        | 25%    |
| `tests_pass` (if applicable)    | 35%    |
| `contains_expected_substrings`  | 15%    |

When a fixture has no test runner (`test_command: null`) the 35% is
redistributed across the other three signals. Scores are always clamped
to `[0.0, 1.0]`.

## Adding a new fixture

1. Create `evals/fixtures/<name>/`.
2. Write `task.json`. `scheme` must be a registered scheme — see
   [`docs/schemes.md`](../docs/schemes.md) for the authoritative list.
3. Drop a minimal repo under `repo/`. Keep it tiny — under ~50 lines of
   source — so fixture runs stay under a second per provider.
4. Write `expected/diff_patterns.json` with the rubric.
5. Run `henchmen eval --provider openai --fixture <name>` to sanity-check
   it, then run a full `henchmen eval --provider openai --write-baseline`
   once you're happy (`--fixture` cannot be combined with the baseline
   flags — a baseline describes a whole run).
6. Add a unit test in `tests/unit/test_evals_harness.py` if the fixture
   exercises a new scoring branch.

## Baseline

`evals/baseline.json` stores the last-known-good aggregate score per
provider, keyed by the canonical provider name. CI runs
`henchmen eval --provider <name> --compare-baseline` and fails if the
aggregate drops by more than 5%.

`--write-baseline` **merges** into the existing entry, so the hand-written
`how_to_populate` and `notes` survive. A run in which any fixture errored —
or in which a declared test runner could not run at all — refuses to write or
compare a baseline and exits 1, so a broken environment can never bake a
0.0 baseline in.
