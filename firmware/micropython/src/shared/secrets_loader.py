"""Loader for /secrets.json — kept separate from config.json so it cannot
leak through any /api/config or Config Builder export path.

Why a dedicated file instead of a sub-key in config.json:
- /api/config explicitly returns the full config (with wifi.password masked).
  Adding the LoRa key to config.json means every existing/future leak vector
  has to remember to mask it. Easy to get wrong.
- A separate file with a single responsibility lets us enforce "never serve
  this, never include in any API response" at the file level. The web server
  already refuses to serve any path not in _STATIC_PATHS / _is_static().
"""
import json


_SECRETS_FILE = "secrets.json"

_loaded = False
_secrets = {}


def _load():
    global _loaded, _secrets
    if _loaded:
        return
    _loaded = True
    try:
        with open(_SECRETS_FILE, "r") as f:
            _secrets = json.load(f)
    except OSError:
        _secrets = {}
    except Exception:
        # Corrupt secrets.json — treat as no secrets rather than crashing the
        # whole boot. The unit will run unauthenticated (logs a warning at
        # the LoRa init site).
        _secrets = {}


def get_lora_key():
    """Return the network HMAC key as bytes, or None if no secrets file.

    Expected secrets.json shape:
        { "lora_key_hex": "<32 hex chars = 16 bytes>" }
    """
    _load()
    hex_key = _secrets.get("lora_key_hex")
    if not hex_key or not isinstance(hex_key, str):
        return None
    try:
        # MicroPython's bytes.fromhex works the same as CPython.
        return bytes.fromhex(hex_key)
    except Exception:
        return None


def has_lora_key():
    return get_lora_key() is not None
