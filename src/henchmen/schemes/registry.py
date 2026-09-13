"""SchemeRegistry - maps scheme IDs to SchemeGraph instances."""

import importlib
import pkgutil
import sys

from henchmen.models.scheme import SchemeDefinition
from henchmen.schemes.base import SchemeGraph

# Modules in this package that never define a scheme, so auto_discover skips them.
_NON_SCHEME_MODULES = frozenset({"base", "registry"})


class SchemeRegistry:
    """Registry mapping scheme_id to SchemeGraph instances."""

    _schemes: dict[str, SchemeGraph] = {}

    @classmethod
    def register(cls, scheme: SchemeDefinition) -> None:
        """Register a scheme. Validates the DAG first.

        Raises ValueError if the scheme is invalid.
        """
        graph = SchemeGraph(scheme)
        errors = graph.validate()
        if errors:
            raise ValueError(f"Scheme '{scheme.id}' is invalid:\n" + "\n".join(f"  - {e}" for e in errors))
        cls._schemes[scheme.id] = graph

    @classmethod
    def get(cls, scheme_id: str) -> SchemeGraph | None:
        """Get a registered scheme graph by ID."""
        return cls._schemes.get(scheme_id)

    @classmethod
    def list_schemes(cls) -> list[str]:
        """List all registered scheme IDs."""
        return list(cls._schemes.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered schemes (useful for testing)."""
        cls._schemes.clear()

    @classmethod
    def auto_discover(cls) -> None:
        """Import all scheme modules in the schemes package to trigger registration."""
        import henchmen.schemes as schemes_pkg

        package_path = schemes_pkg.__path__
        package_name = schemes_pkg.__name__

        for module_info in pkgutil.iter_modules(package_path):
            module_name = f"{package_name}.{module_info.name}"
            # Skip the machinery and the private helper modules (shared prompt
            # templates, pipeline factories) — they register nothing, and
            # reloading them would rebuild constants the scheme modules hold.
            if module_info.name in _NON_SCHEME_MODULES or module_info.name.startswith("_"):
                continue
            if module_name in sys.modules:
                # Module already loaded — reload to re-execute module-level registrations
                importlib.reload(sys.modules[module_name])
            else:
                importlib.import_module(module_name)
