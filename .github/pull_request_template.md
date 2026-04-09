<!--
Thanks for contributing to Henchmen. Please fill out each section below.
Keep the summary tight and link issues so reviewers have full context.
-->

## Summary

<!-- What does this PR do, and why? 1-3 sentences is plenty. -->

## Related issue(s)

<!-- Use "Fixes #123" to auto-close, or "Relates to #123" for partial work. -->

- Fixes #
- Relates to #

## Type of change

- [ ] Bug fix
- [ ] Feature
- [ ] Refactor
- [ ] Docs
- [ ] Test
- [ ] Infra / Terraform / CI

## Quality checklist

- [ ] `ruff check src/ tests/` passes
- [ ] `ruff format src/ tests/` applied
- [ ] `mypy src/` passes under strict mode
- [ ] `pytest tests/unit/` passes
- [ ] New or changed code has accompanying tests

## Deploy impact

<!-- Flag anything a reviewer or operator needs to know before merging. -->

- [ ] Changes Terraform / infrastructure
- [ ] Changes Cloud Run service or job config
- [ ] Adds or rotates secrets (Secret Manager)
- [ ] Requires a migration or one-time backfill
- [ ] None of the above

## Reviewer notes

<!-- Call out anything non-obvious: tricky diffs, known follow-ups, areas that need extra scrutiny. -->
