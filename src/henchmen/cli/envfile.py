"""Read, update and atomically write ``.env.local`` files.

``henchmen init`` uses this to persist configuration. The parser keeps every
line it does not understand (comments, blank lines, unknown keys) exactly as
written, updates known keys in place, appends new keys under a section
header, and writes the result atomically and owner-only.

Both the main file and its ``.bak`` backup go through
:mod:`henchmen.config.secret_files` (``O_CREAT | O_EXCL | O_WRONLY``, mode
0600, an atomic ``os.replace`` retried against a transient Windows
``PermissionError``), so every writer gets the same guarantee: a backup that
already existed with looser permissions is replaced by an owner-only one, and
a write interrupted partway through never corrupts the previous content.

The dialect matches what pydantic-settings / python-dotenv read: ``KEY=value``,
optional ``export`` prefix, single or double quotes, ``#`` comments.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from henchmen.config.secret_files import write_secret_file

_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_NEEDS_QUOTES_RE = re.compile(r"[\s#\"'\\$`]")

_SECRET_KEY_SUFFIXES: tuple[str, ...] = ("_TOKEN", "_API_KEY", "_PRIVATE_KEY", "_SECRET", "_PASSWORD")


def is_secret_key(key: str) -> bool:
    """True when a dotenv key names a credential whose value must never be displayed.

    Only the name's ending counts: ``HENCHMEN_GITHUB_TOKEN`` and
    ``HENCHMEN_ANTHROPIC_API_KEY`` are secret, while
    ``HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS``, ``HENCHMEN_JIRA_PROJECT_KEY`` and
    ``HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH`` are not.

    The single secret-name classifier: ``henchmen.cli.config_cmd.is_secret_field``
    (``henchmen config``'s masking) delegates to this function, as does
    :meth:`henchmen.console.config_store.ConfigStore.masked`, so every surface
    that decides whether to show a Settings field's value agrees.

    ``henchmen.console.app``'s setup-state choice filter (``_choices_are_not_secrets``)
    intentionally checks a broader set of name segments and suffixes than this
    function -- a false positive there only blocks saving a non-secret choice
    name, while a false negative here would display a credential, so the two
    are deliberately not unified.
    """
    return key.strip().upper().endswith(_SECRET_KEY_SUFFIXES)


def _unquote(raw: str) -> str:
    """Strip surrounding quotes and inline comments from a raw assignment value."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    # Unquoted: an inline comment starts at " #" (space then hash).
    hash_index = value.find(" #")
    if hash_index != -1:
        value = value[:hash_index]
    return value.strip()


def quote_value(value: str) -> str:
    """Render ``value`` so that it round-trips through dotenv parsing."""
    if value == "":
        return '""'
    if _NEEDS_QUOTES_RE.search(value) is None:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class _Line:
    raw: str
    key: str | None = None
    value: str | None = None


@dataclass
class EnvFile:
    """An ordered, comment-preserving view of a dotenv file."""

    path: Path
    exists: bool = False
    _lines: list[_Line] = field(default_factory=list)

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str) -> EnvFile:
        """Load ``path``; a missing file yields an empty EnvFile."""
        path = Path(path)
        if not path.is_file():
            return cls(path=path, exists=False)
        env = cls.from_text(path.read_text(encoding="utf-8"))
        env.path = path
        env.exists = True
        return env

    @classmethod
    def from_text(cls, text: str, path: Path | str = ".env.local") -> EnvFile:
        """Parse dotenv ``text`` (used for round-trip tests)."""
        lines: list[_Line] = []
        for raw in text.splitlines():
            match = _ASSIGNMENT_RE.match(raw)
            if match:
                lines.append(_Line(raw=raw, key=match.group(1), value=_unquote(match.group(2))))
            else:
                lines.append(_Line(raw=raw))
        return cls(path=Path(path), exists=False, _lines=lines)

    # -- read --------------------------------------------------------------

    def get(self, key: str, default: str = "") -> str:
        """Return the value of ``key``; the last assignment wins, like dotenv."""
        value = default
        for line in self._lines:
            if line.key == key and line.value is not None:
                value = line.value
        return value

    def keys(self) -> list[str]:
        """Assigned keys in first-appearance order."""
        seen: list[str] = []
        for line in self._lines:
            if line.key is not None and line.key not in seen:
                seen.append(line.key)
        return seen

    def as_dict(self) -> dict[str, str]:
        return {key: self.get(key) for key in self.keys()}

    # -- write -------------------------------------------------------------

    def set(self, key: str, value: str, *, section: str | None = None) -> None:
        """Assign ``key``; update the first occurrence in place, drop later duplicates, or append."""
        rendered = f"{key}={quote_value(value)}"
        first_index = next((i for i, line in enumerate(self._lines) if line.key == key), None)
        if first_index is not None:
            self._lines[first_index] = _Line(raw=rendered, key=key, value=value)
            self._lines = [line for i, line in enumerate(self._lines) if line.key != key or i == first_index]
            return
        if section:
            self._ensure_section(section)
            insert_at = self._section_end(section)
            self._lines.insert(insert_at, _Line(raw=rendered, key=key, value=value))
            return
        self._lines.append(_Line(raw=rendered, key=key, value=value))

    def set_many(self, values: Mapping[str, str], *, section: str | None = None) -> None:
        for key, value in values.items():
            self.set(key, value, section=section)

    def unset(self, key: str) -> None:
        self._lines = [line for line in self._lines if line.key != key]

    def render(self) -> str:
        """Serialise back to text with exactly one trailing newline."""
        body = "\n".join(line.raw for line in self._lines).rstrip("\n")
        return body + "\n" if body else ""

    def write(self, *, backup: bool = True) -> Path | None:
        """Atomically write to :attr:`path`, owner-only. Returns the backup path if one was made.

        The backup (when requested and a previous file exists) is written
        first, from the complete previous content, before the main file is
        touched -- so an interruption during the main write can never lose
        the previous content, and an existing world-readable ``.bak`` is
        replaced by an owner-only one rather than reused in place.
        """
        backup_path: Path | None = None
        if backup and self.path.is_file():
            backup_path = self.path.with_name(self.path.name + ".bak")
            write_secret_file(backup_path, self.path.read_bytes())

        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_secret_file(self.path, self.render().encode("utf-8"))
        self.exists = True
        return backup_path

    # -- sections ----------------------------------------------------------

    @staticmethod
    def _header(section: str) -> str:
        return f"# ---- {section} ----"

    def _ensure_section(self, section: str) -> None:
        header = self._header(section)
        if any(line.raw.strip() == header for line in self._lines):
            return
        if self._lines and self._lines[-1].raw.strip():
            self._lines.append(_Line(raw=""))
        self._lines.append(_Line(raw=header))

    def _section_end(self, section: str) -> int:
        """Index just past the contiguous block of assignments under ``section``'s header."""
        header = self._header(section)
        start = next(i for i, line in enumerate(self._lines) if line.raw.strip() == header)
        end = start + 1
        while end < len(self._lines) and self._lines[end].key is not None:
            end += 1
        return end
