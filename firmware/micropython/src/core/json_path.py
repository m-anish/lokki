"""Tiny JSON-path get/set utility.

Path syntax: slash-separated keys, no leading slash, numeric segments
walk into lists. Examples:

    "system/unit_name"                                "Pagoda"
    "led_channels/2/default_duty_percent"             80
    "led_channels/0/time_windows"                     [...]
    "led_channels"                                    [...full array...]

Used by the incremental config protocol: the coord builds a patch
`{path, value}` referring to a path on the leaf's config; the leaf
walks its in-memory config and applies the value at that path.

Set-mode auto-vivifies intermediate dicts (`a/b/c = 1` on `{}` will
create `{"a": {"b": {"c": 1}}}`) but does NOT auto-vivify lists,
because list indices must point at existing slots — the alternative
introduces silent ordering bugs (setting `led_channels/5/foo` on a
3-element list would either fail or grow to 6 with two undefined
slots; either is worse than refusing).

Returns are explicit — `(ok, value_or_reason)` — instead of raising,
so callers can surface the failure as a user-facing error string
without exception-handler boilerplate at every site.
"""


def _split(path):
    """Return list of segments. Numeric segments become ints so dict
    lookup vs list-index dispatch works at the walk site."""
    parts = []
    for seg in path.split("/"):
        if seg == "":
            # Empty segment from leading/trailing/double slash. Reject
            # rather than silently coalesce — it's almost always a bug.
            return None
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(seg)
    return parts


def get_at(obj, path):
    """Walk `obj` to `path`. Returns (ok, value_or_reason).

    `ok=False` cases: empty/malformed path, segment refers to missing
    key on a dict, segment is non-int on a list, segment is out of
    range on a list, walk passes through a leaf (int/str/None) before
    consuming all segments.
    """
    if not isinstance(path, str) or not path:
        return False, "empty path"
    segments = _split(path)
    if segments is None:
        return False, f"malformed path: {path!r}"
    cur = obj
    for i, seg in enumerate(segments):
        if isinstance(cur, dict):
            if not isinstance(seg, str):
                return False, f"path: numeric segment at {'/'.join(str(s) for s in segments[:i+1])} but parent is a dict"
            if seg not in cur:
                return False, f"path: key {seg!r} not found at {'/'.join(str(s) for s in segments[:i])}"
            cur = cur[seg]
        elif isinstance(cur, list):
            if not isinstance(seg, int):
                return False, f"path: string segment {seg!r} but parent is a list"
            if seg < 0 or seg >= len(cur):
                return False, f"path: index {seg} out of range (list length {len(cur)})"
            cur = cur[seg]
        else:
            return False, f"path: cannot walk into {_typename(cur)} at {'/'.join(str(s) for s in segments[:i])}"
    return True, cur


def set_at(obj, path, value):
    """Set `value` at `path` in `obj`. Mutates `obj` in place. Returns
    (ok, error_or_none).

    `ok=False` cases: same as get_at for malformed paths, plus list
    indices must be in range (no auto-grow).
    """
    if not isinstance(path, str) or not path:
        return False, "empty path"
    segments = _split(path)
    if segments is None:
        return False, f"malformed path: {path!r}"
    if not segments:
        return False, "empty path"

    # Walk to the parent of the final segment, creating intermediate
    # dicts on demand. Lists must already exist — we never auto-create
    # them or auto-grow them, because doing so silently introduces
    # off-by-one bugs in positional-id arrays.
    cur = obj
    for i, seg in enumerate(segments[:-1]):
        if isinstance(cur, dict):
            if not isinstance(seg, str):
                return False, f"path: numeric segment at {'/'.join(str(s) for s in segments[:i+1])} but parent is a dict"
            if seg not in cur:
                # Auto-vivify intermediate dicts. The next segment
                # determines whether we need a dict or refuse.
                next_seg = segments[i + 1]
                if isinstance(next_seg, int):
                    return False, f"path: would auto-create list at {'/'.join(str(s) for s in segments[:i+1])} (not supported)"
                cur[seg] = {}
            cur = cur[seg]
        elif isinstance(cur, list):
            if not isinstance(seg, int):
                return False, f"path: string segment {seg!r} but parent is a list"
            if seg < 0 or seg >= len(cur):
                return False, f"path: index {seg} out of range (list length {len(cur)})"
            cur = cur[seg]
        else:
            return False, f"path: cannot walk into {_typename(cur)} at {'/'.join(str(s) for s in segments[:i])}"

    # Assign at the final segment.
    last = segments[-1]
    if isinstance(cur, dict):
        if not isinstance(last, str):
            return False, f"path: final numeric segment but parent is a dict"
        cur[last] = value
        return True, None
    if isinstance(cur, list):
        if not isinstance(last, int):
            return False, f"path: final string segment but parent is a list"
        if last < 0 or last >= len(cur):
            return False, f"path: final index {last} out of range (list length {len(cur)})"
        cur[last] = value
        return True, None
    return False, f"path: cannot set into {_typename(cur)}"


def _typename(v):
    if v is None:                 return "null"
    if isinstance(v, bool):       return "boolean"
    if isinstance(v, int):        return "integer"
    if isinstance(v, float):      return "number"
    if isinstance(v, str):        return "string"
    if isinstance(v, list):       return "array"
    if isinstance(v, dict):       return "object"
    return type(v).__name__
