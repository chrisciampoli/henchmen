"""Token estimation for the Operative runtime.

Budgets (system prompt size, dossier trimming, per-message truncation) are
char-based on purpose: they run on every step of the agent loop and must not
make a network call. ``4 chars ≈ 1 token`` is close enough for a budget and
cannot stall the event loop.

Exact counts, when a caller genuinely needs them, come from the provider:
``await provider.count_tokens(text, model)``.
"""


def estimate_tokens(text: str) -> int:
    """Quick token estimate without an SDK call (4 chars ≈ 1 token)."""
    return len(text) // 4 if text else 0
