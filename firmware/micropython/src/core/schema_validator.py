"""Tiny JSON Schema validator — MicroPython-friendly subset.

Supports the constraints we actually use in `config.schema.json`:

  type, required, properties, additionalProperties (only `false`),
  items, minItems, maxItems, minimum, maximum, minLength, maxLength,
  enum, pattern, if/then/else.

Deliberately NOT supported (so the schema author can't write something
this validator will silently ignore):

  $ref, allOf, anyOf, oneOf, not, dependencies, format, const, multipleOf,
  uniqueItems, additionalProperties: <schema>, prefixItems, contains,
  patternProperties.

If you need any of those, either extend this file or move the rule into
`semantic_checks.py`. We deliberately keep this small — every constraint
this file supports has a one-line handler.

Number/integer/boolean handling: Python's `bool` is a subclass of `int`,
which would let `true` validate as `type: "integer"`. We special-case
that to match JSON Schema semantics.

Returns a list of error message strings on validation failure. Empty
list means valid. Path-prefixed messages make errors operator-readable:

    system.unit_id must be one of [0,1,2,3,4,5,6,7,8,99]
"""
import re


# --- JSON-Schema type → Python type predicate -----------------------------

def _is_type(value, t):
    if t == "object":   return isinstance(value, dict)
    if t == "array":    return isinstance(value, list)
    if t == "string":   return isinstance(value, str)
    if t == "boolean":  return isinstance(value, bool)
    if t == "integer":
        # bool is a subclass of int in Python; exclude it.
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        # ints and floats both count; exclude bool.
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if t == "null":     return value is None
    return False


def _matches(value, schema):
    """Used by `if`-schemas: does `value` pass `schema` *quietly*? No
    error accumulation, just a yes/no. We probe with the full validator
    and return True iff it produced no errors."""
    return len(validate(value, schema, "")) == 0


def _path_join(prefix, key):
    if prefix == "":
        return key
    if isinstance(key, int) or (isinstance(key, str) and key.startswith("[")):
        return f"{prefix}{key}"
    return f"{prefix}.{key}"


# --- The main recursive validator ----------------------------------------

def validate(value, schema, path=""):
    """Validate `value` against `schema`. Returns a list of error
    strings (empty list = valid). `path` is the dotted/bracketed key
    path used in error messages."""
    errors = []
    _validate_into(value, schema, path, errors)
    return errors


def _validate_into(value, schema, path, errors):
    # Bool/None shortcut schemas: {true} accepts anything, {false}
    # rejects anything. Not in our subset (we use full objects), but
    # cheap to handle.
    if schema is True:
        return
    if schema is False:
        errors.append(f"{path or '<root>'}: nothing is allowed here")
        return
    if not isinstance(schema, dict):
        return

    # type
    if "type" in schema:
        t = schema["type"]
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, tt) for tt in types):
            type_label = "/".join(types)
            errors.append(f"{path or '<root>'} must be {type_label} (got {_friendly_type(value)})")
            # Don't run further constraints — they'd cascade meaningless errors.
            return

    # enum
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path or '<root>'} must be one of {schema['enum']} (got {value!r})")

    # pattern (strings only)
    if "pattern" in schema and isinstance(value, str):
        try:
            if not re.match(schema["pattern"], value):
                errors.append(f"{path or '<root>'} must match pattern '{schema['pattern']}' (got {value!r})")
        except Exception as e:
            errors.append(f"{path or '<root>'} pattern check failed: {e}")

    # Numeric bounds
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path or '<root>'} must be >= {schema['minimum']} (got {value})")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path or '<root>'} must be <= {schema['maximum']} (got {value})")

    # String length
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path or '<root>'} must be at least {schema['minLength']} char(s)")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path or '<root>'} must be at most {schema['maxLength']} char(s)")

    # Object: required + properties + additionalProperties:false
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{_path_join(path, key)} is required")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                _validate_into(value[key], subschema, _path_join(path, key), errors)
        # additionalProperties — we only support `false` (reject unknowns)
        # and `true` (allow, the default). Don't bother with subschema form.
        if schema.get("additionalProperties") is False:
            known = set(props.keys())
            for key in value.keys():
                if key not in known:
                    errors.append(f"{_path_join(path, key)} is not a known property")

    # Array: items + minItems/maxItems
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path or '<root>'} must have at least {schema['minItems']} item(s) (got {len(value)})")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path or '<root>'} must have at most {schema['maxItems']} item(s) (got {len(value)})")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate_into(item, schema["items"], f"{path}[{i}]", errors)

    # if/then/else — JSON Schema 2019-09+ conditional. Match if-schema
    # against value (quietly); if it matches, then-schema must also
    # match; otherwise else-schema must match (if present).
    if "if" in schema:
        if _matches(value, schema["if"]):
            if "then" in schema:
                _validate_into(value, schema["then"], path, errors)
        else:
            if "else" in schema:
                _validate_into(value, schema["else"], path, errors)


def _friendly_type(value):
    if value is None:                  return "null"
    if isinstance(value, bool):        return "boolean"
    if isinstance(value, int):         return "integer"
    if isinstance(value, float):       return "number"
    if isinstance(value, str):         return "string"
    if isinstance(value, list):        return "array"
    if isinstance(value, dict):        return "object"
    return type(value).__name__
