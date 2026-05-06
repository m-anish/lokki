"""Minimal HMAC-SHA256 for MicroPython.

MicroPython ships hashlib.sha256 but not the `hmac` module. This implements
the standard RFC 2104 construction directly. Verified against CPython's
hmac.new(key, msg, sha256).digest() for several test vectors.

Block size of SHA-256 = 64 bytes. Output = 32 bytes (we truncate to 8 in
the LoRa wire layer to keep envelope overhead low).
"""
import hashlib


_BLOCK_SIZE = 64


def hmac_sha256(key, msg):
    """Return the 32-byte HMAC-SHA256 of msg under key.

    `key` and `msg` must be bytes-like. Returns bytes.
    """
    if isinstance(key, str):
        key = key.encode()
    if isinstance(msg, str):
        msg = msg.encode()

    # If key is longer than block size, hash it down. If shorter, pad.
    if len(key) > _BLOCK_SIZE:
        key = hashlib.sha256(key).digest()
    if len(key) < _BLOCK_SIZE:
        key = key + b"\x00" * (_BLOCK_SIZE - len(key))

    o_pad = bytes(b ^ 0x5c for b in key)
    i_pad = bytes(b ^ 0x36 for b in key)

    inner = hashlib.sha256(i_pad + msg).digest()
    return hashlib.sha256(o_pad + inner).digest()
