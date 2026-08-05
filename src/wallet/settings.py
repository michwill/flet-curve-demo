"""Local, per-installation configuration.

Read from `src/local_config.toml` (gitignored) with environment variables
taking precedence, so CI can set values without a file.

Why the file lives under `src/` rather than at the repo root: `flet publish`
tars the *script directory* into the archive Pyodide unpacks, so anything
under `src/` is readable by Python in the browser and anything outside it is
not. Keeping it here means a plain `flet publish` produces a correctly
configured build -- no post-processing step to forget.

That matters more than it sounds. The first version of this put the value in
a JS file and patched it in after the build; skipping the wrapper script
produced a build that looked fine and silently dropped WalletConnect. Config
that only works if you remember an extra step is config that will be wrong.

NOTHING HERE IS A SECRET. Everything in this file is bundled into the app
and served to every visitor. A WalletConnect projectId is public by design
-- it is account-scoped, so it is kept out of the repo to avoid a clone
spending your quota, not to hide it. Do not put real credentials here.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parents[1] / "local_config.toml"

#: TOML key -> environment variable, per section.
_ENV = {
    ("walletconnect", "project_id"): "FLET_PAY_WALLETCONNECT_PROJECT_ID",
    ("walletconnect", "module_url"): "FLET_PAY_WALLETCONNECT_MODULE_URL",
    ("walletconnect", "icon"): "FLET_PAY_WALLETCONNECT_ICON",
}

#: TOML key -> the key the JS bridge expects.
_BRIDGE_KEYS = {
    ("walletconnect", "project_id"): "walletConnectProjectId",
    ("walletconnect", "module_url"): "walletConnectModuleUrl",
    ("walletconnect", "icon"): "walletConnectIcon",
    ("walletconnect", "chains"): "walletConnectChains",
}


@lru_cache(maxsize=1)
def _file_values() -> dict[str, Any]:
    """Parse the config file. Missing or malformed is not fatal.

    An example app must still start when the file is absent -- that is the
    fresh-clone case -- and a typo in it should degrade to "unconfigured"
    rather than a traceback on launch.
    """
    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"[wallet] ignoring {CONFIG_PATH.name}: {exc}")
        return {}


def get(section: str, key: str, default: Any = None) -> Any:
    """One setting: environment first, then the file, then `default`."""
    env_var = _ENV.get((section, key))
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    value = _file_values().get(section, {}).get(key, default)
    return default if value in ("", []) else value


def bridge_config() -> dict[str, Any]:
    """Settings to hand to the browser bridge, in its own key names.

    Only non-empty values are included, so the bridge keeps its built-in
    defaults for everything unset.
    """
    resolved: dict[str, Any] = {}
    for (section, key), bridge_key in _BRIDGE_KEYS.items():
        value = get(section, key)
        if value not in (None, "", []):
            resolved[bridge_key] = value
    return resolved


def describe() -> str:
    """One line for logs: what was configured, without dumping the values."""
    keys = sorted(bridge_config())
    if not keys:
        return f"no local config ({CONFIG_PATH.name} absent or empty)"
    return f"local config: {', '.join(keys)}"
