"""Driver registry and lab configuration loading.

Two mechanisms let you add instrument N+1 without editing framework code:

  1. ``@register_driver("vendor.model")`` on your Device subclass.
  2. A ``labaiagent.drivers`` entry point in your own package's pyproject, so a
     driver can ship as a separate pip-installable package. Site-specific
     drivers stay in your repo; nothing has to be upstreamed.

The lab config is a plain YAML file listing instruments. Adding a device is a
five-line stanza -- no code change anywhere.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .device import Device
from .errors import ConfigurationError

_REGISTRY: dict[str, type[Device]] = {}
_ALIASES: dict[str, str] = {}


def register_driver(key: str, *aliases: str) -> Callable[[type[Device]], type[Device]]:
    """Class decorator registering a driver under a dotted key."""
    def deco(cls: type[Device]) -> type[Device]:
        if not issubclass(cls, Device):
            raise TypeError(f"{cls.__name__} must subclass labaiagent.Device")
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ConfigurationError(
                f"Driver key {key!r} already registered to "
                f"{_REGISTRY[key].__name__}; refusing to shadow it."
            )
        _REGISTRY[key] = cls
        for a in aliases:
            _ALIASES[a] = key
        cls.driver_key = key  # type: ignore[attr-defined]
        return cls
    return deco


def get_driver(key: str) -> type[Device]:
    k = _ALIASES.get(key, key)
    if k not in _REGISTRY:
        load_builtin_drivers()
        load_plugin_drivers()
    k = _ALIASES.get(key, key)
    try:
        return _REGISTRY[k]
    except KeyError:
        near = [d for d in _REGISTRY if key.split(".")[0] in d]
        raise ConfigurationError(
            f"No driver registered for {key!r}."
            + (f" Similar: {sorted(near)}." if near else "")
            + f" Known drivers: {sorted(_REGISTRY)}"
        ) from None


def list_drivers() -> dict[str, dict[str, str]]:
    load_builtin_drivers()
    load_plugin_drivers()
    return {
        k: {"class": c.__name__, "vendor": c.vendor, "model": c.model,
            "category": c.category, "module": c.__module__,
            "version": c.driver_version}
        for k, c in sorted(_REGISTRY.items())
    }


_builtins_loaded = False
_plugins_loaded = False


def load_builtin_drivers() -> None:
    """Import every module under ``labaiagent.drivers`` so decorators fire."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    try:
        import labaiagent.drivers as pkg
    except ImportError:  # pragma: no cover
        return
    for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # a broken optional driver must not kill the app
            import warnings
            warnings.warn(f"Skipping driver module {mod.name}: {exc}",
                          stacklevel=2)


def load_plugin_drivers() -> None:
    """Discover third-party drivers advertised via the ``labaiagent.drivers``
    entry-point group."""
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    try:
        eps = md.entry_points(group="labaiagent.drivers")
    except Exception:  # pragma: no cover
        return
    for ep in eps:
        try:
            ep.load()
        except Exception as exc:  # pragma: no cover
            import warnings
            warnings.warn(f"Failed to load driver plugin {ep.name}: {exc}",
                          stacklevel=2)


# --------------------------------------------------------------------------
# Lab configuration
# --------------------------------------------------------------------------

def build_device(spec: dict[str, Any]) -> Device:
    """Instantiate one device from a config stanza."""
    if "id" not in spec:
        raise ConfigurationError(f"Device stanza missing 'id': {spec}")
    if "driver" not in spec:
        raise ConfigurationError(f"Device {spec['id']!r} missing 'driver'")
    cls = get_driver(spec["driver"])
    return cls(
        spec["id"],
        config=spec.get("config", {}),
        location=spec.get("location", ""),
        tags=spec.get("tags", ()),
        simulated=bool(spec.get("simulated", False)),
    )


def load_lab_config(path: str | Path) -> dict[str, Any]:
    """Read and lightly validate a lab YAML file."""
    import yaml

    p = Path(path)
    if not p.exists():
        raise ConfigurationError(f"Lab config not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if "devices" not in data:
        raise ConfigurationError(f"{p}: config must contain a top-level 'devices' list")
    seen: set[str] = set()
    for d in data["devices"]:
        did = d.get("id")
        if did in seen:
            raise ConfigurationError(f"{p}: duplicate device id {did!r}")
        seen.add(did)
    return data


def devices_from_config(path: str | Path) -> list[Device]:
    cfg = load_lab_config(path)
    return [build_device(d) for d in cfg["devices"]]


__all__ = [
    "register_driver", "get_driver", "list_drivers", "build_device",
    "load_lab_config", "devices_from_config", "load_builtin_drivers",
    "load_plugin_drivers",
]
