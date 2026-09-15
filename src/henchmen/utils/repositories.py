"""Repository names: the one rule for turning the configured default into ``owner/name``.

``github_default_repo`` is saved by the Console as ``owner/name``, but older
configurations hold a bare name next to ``github_default_org``. Dispatch's
normalizer, ``henchmen chat`` and ``henchmen doctor`` all resolve it here, so the
rule exists once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

DEFAULT_REPO_PROBLEM = "The default repository must be owner/name (set HENCHMEN_GITHUB_DEFAULT_REPO=owner/name)"


def qualify_repo(repo: str, org: str) -> str:
    """``repo`` as ``owner/name``: a bare name gets ``org`` as its owner when ``org`` is set.

    An empty ``repo`` stays empty (never ``"org/"``). A name that already has a
    ``/``, or a bare name with no ``org``, is returned unchanged (stripped); use
    :func:`is_owner_name` to tell whether the result is usable.
    """
    name = (repo or "").strip()
    owner = (org or "").strip()
    if not name or "/" in name or not owner:
        return name
    return f"{owner}/{name}"


def is_owner_name(repo: str) -> bool:
    """True for exactly two non-empty ``/``-separated parts."""
    parts = (repo or "").split("/")
    return len(parts) == 2 and all(part.strip() for part in parts)


def default_repository(settings: Settings) -> str:
    """The configured default repository, qualified with ``github_default_org`` when it is a bare name."""
    return qualify_repo(settings.github_default_repo, settings.github_default_org)
