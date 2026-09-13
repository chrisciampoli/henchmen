"""The repository an Arsenal tool is currently operating on.

``REPO_URL`` is part of the Operative runtime contract (the Lair injects it
next to ``TASK_ID`` / ``BRANCH`` / ``WORKSPACE_DIR``), so it is the one place
a tool can learn which repo it was dispatched against. Everything else —
tokens, regions, defaults — comes from :class:`~henchmen.config.settings.Settings`.
"""

from __future__ import annotations

import os

_GIT_SUFFIX = ".git"
_HTTP_PREFIXES = ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/")


def normalize_repo_slug(repo_url: str) -> str:
    """Return ``owner/repo`` for a GitHub URL, or ``""`` when it is not one.

    Accepts the forms the intake handlers produce: a full HTTPS URL, an SSH
    remote, or an already-normalised ``owner/repo`` slug.
    """
    value = (repo_url or "").strip()
    if not value:
        return ""
    for prefix in _HTTP_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    value = value.strip("/")
    if value.endswith(_GIT_SUFFIX):
        value = value[: -len(_GIT_SUFFIX)]
    parts = [p for p in value.split("/") if p]
    if len(parts) != 2:
        return ""
    return "/".join(parts)


def current_repo_slug() -> str:
    """Return the ``owner/repo`` the Operative is working on.

    Prefers the ``REPO_URL`` runtime contract variable and falls back to
    ``settings.github_default_repo``. Returns ``""`` when neither is usable so
    callers can raise a clear error instead of calling an API with a blank
    repository name.
    """
    from henchmen.config.settings import get_settings

    slug = normalize_repo_slug(os.environ.get("REPO_URL", ""))
    if slug:
        return slug
    return normalize_repo_slug(get_settings().github_default_repo)
