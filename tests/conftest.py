"""Pytest configuration ensuring `mutmut` imports isolated mutated copies under `mutants/`.

Strict no-op outside `mutmut`: activates ONLY when `MUTANT_UNDER_TEST` is present in
`os.environ` AND the current working directory is `mutants/`. Normal pytest runs ALWAYS
import the real source modules directly.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

_CWD = Path.cwd()
if "MUTANT_UNDER_TEST" in os.environ and _CWD.name == "mutants" and (_CWD / "sentinel").is_dir():
    _MUTANTS_ROOT = str(_CWD)
    if _MUTANTS_ROOT not in sys.path:
        sys.path.insert(0, _MUTANTS_ROOT)

    _ALIASES: dict[str, str] = {
        "sentinel_common": "sentinel.common",
        "sentinel_mcp": "sentinel.mcp_server.sentinel_mcp",
        "sentinel_worker": "sentinel.worker.sentinel_worker",
        "sentinel_api": "sentinel.api.sentinel_api",
        "rollback_executor": "sentinel.rollback_executor.rollback_executor",
        "restart_executor": "sentinel.restart_executor.restart_executor",
        "sentinelctl": "cli.sentinelctl.sentinelctl",
    }

    class _MutmutAliasLoader(importlib.abc.Loader):
        def __init__(self, target_name: str) -> None:
            self._target_name = target_name

        def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
            return importlib.import_module(self._target_name)

        def exec_module(self, module: ModuleType) -> None:
            return None

    class _MutmutAliasFinder(importlib.abc.MetaPathFinder):
        def find_spec(
            self,
            fullname: str,
            path: object | None = None,
            target: ModuleType | None = None,
        ) -> importlib.machinery.ModuleSpec | None:
            for short_pkg, canonical_pkg in _ALIASES.items():
                if fullname == short_pkg or fullname.startswith(f"{short_pkg}."):
                    suffix = fullname[len(short_pkg) :]
                    canonical = f"{canonical_pkg}{suffix}"
                    real_spec = importlib.util.find_spec(canonical)
                    if real_spec is None:
                        return None
                    return importlib.machinery.ModuleSpec(
                        name=fullname,
                        loader=_MutmutAliasLoader(canonical),
                        origin=real_spec.origin,
                        is_package=real_spec.submodule_search_locations is not None,
                    )
            return None

    for _mod in list(sys.modules):
        if any(_mod == k or _mod.startswith(f"{k}.") for k in _ALIASES):
            sys.modules.pop(_mod, None)

    sys.meta_path.insert(0, _MutmutAliasFinder())
