"""RFC 6902 style JSON Patch application on a private document copy.

Only the three structural operations ``add``, ``remove`` and ``replace`` are
supported.  Paths are RFC 6901 JSON Pointers; the last token may be ``-`` to
mean "one past the end" for ``add`` on an array.

Validation happens before any mutation is observable:

1. every operation is statically checked (shape, op, pointer syntax, value);
2. the operations are then applied in order to a deep copy of the document,
   one by one — each step checks its parent container, object member and
   array index against the document *as it stands after the previous
   successful steps*;
3. the first failing step raises an :class:`~app.errors.ApiError` located at
   ``operations[i]`` and the caller's copy is discarded, so no partial
   result is ever returned.

Only when every step succeeds is the patched copy returned together with the
RFC 6901 pointer of every touched location.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

from . import pointers
from .errors import (
    ApiError,
    INVALID_REQUEST,
    PATH_NOT_FOUND,
    POINTER_SYNTAX,
)
from .validation import validate_json_value

SUPPORTED_OPS = ("add", "remove", "replace")


def _array_index(token: str, length: int, *, append: bool, loc: str) -> int:
    """Validate an array reference token against an array of *length*.

    With *append* true the RFC 6902 ``-`` token (one past the end) is accepted
    and maps to ``length``; otherwise indices must satisfy ``0 <= i < length``.
    Leading-zero tokens like ``01`` are never array indices.
    """
    if token == "-":
        if append:
            return length
        raise ApiError(
            PATH_NOT_FOUND,
            f"token '-' only addresses the position one past the end of an "
            f"array and cannot be removed or replaced (array length {length})",
            loc=loc,
            detail={"token": "-", "arrayLength": length},
        )
    if token == "0":
        idx = 0
    elif token.isdigit() and not token.startswith("0"):
        idx = int(token)
    else:
        raise ApiError(
            PATH_NOT_FOUND,
            f"token {token!r} is not a valid array index ('0' or digits "
            "without a leading zero)",
            loc=loc,
            detail={"token": token},
        )
    upper = length if append else length - 1
    if idx > upper:
        raise ApiError(
            PATH_NOT_FOUND,
            f"array index {idx} out of range for array of length {length} "
            f"(valid range 0..{upper})",
            loc=loc,
            detail={"index": idx, "arrayLength": length},
        )
    return idx


def _static_check(raw_ops: Any) -> List[Dict[str, Any]]:
    """Validate the ``operations`` array shape that is independent of the doc.

    Each surviving entry is normalized to ``(op, path_string, tokens, value)``.
    """
    if not isinstance(raw_ops, list):
        raise ApiError(
            INVALID_REQUEST,
            "'operations' must be an array of {op, path, value?} objects",
            loc="operations",
        )

    checked: List[Dict[str, Any]] = []
    for i, raw in enumerate(raw_ops):
        loc = f"operations[{i}]"
        if not isinstance(raw, dict):
            raise ApiError(
                INVALID_REQUEST, "patch operation must be an object", loc=loc
            )

        op = raw.get("op")
        if "op" not in raw:
            raise ApiError(
                INVALID_REQUEST, "missing required field 'op'", loc=f"{loc}.op"
            )
        if not isinstance(op, str):
            raise ApiError(
                INVALID_REQUEST,
                f"'op' must be a string, got {type(op).__name__}",
                loc=f"{loc}.op",
            )
        if op not in SUPPORTED_OPS:
            raise ApiError(
                INVALID_REQUEST,
                f"unsupported op {op!r}: only 'add', 'remove' and 'replace' "
                "are supported",
                loc=f"{loc}.op",
                detail={"op": op, "supported": list(SUPPORTED_OPS)},
            )

        if "path" not in raw:
            raise ApiError(
                INVALID_REQUEST,
                "missing required field 'path'",
                loc=f"{loc}.path",
            )
        path = raw["path"]
        if not isinstance(path, str):
            raise ApiError(
                INVALID_REQUEST,
                f"'path' must be an RFC 6901 pointer string, got "
                f"{type(path).__name__}",
                loc=f"{loc}.path",
            )
        try:
            tokens = pointers.parse_pointer(path)
        except pointers.PointerError as exc:
            raise ApiError(POINTER_SYNTAX, str(exc), loc=f"{loc}.path") from exc

        value: Any = None
        if op in ("add", "replace"):
            if "value" not in raw:
                raise ApiError(
                    INVALID_REQUEST,
                    f"'{op}' operation requires a 'value' field",
                    loc=f"{loc}.value",
                )
            value = raw["value"]
            validate_json_value(value, loc=f"{loc}.value")
        elif "value" in raw:
            raise ApiError(
                INVALID_REQUEST,
                "'remove' operation must not carry a 'value' field",
                loc=f"{loc}.value",
            )

        checked.append({"op": op, "path": path, "tokens": tokens, "value": value})
    return checked


def _apply_add_replace(
    doc: Any,
    tokens: Tuple[str | int, ...],
    value: Any,
    *,
    op: str,
    loc: str,
) -> Any:
    """Apply ``add``/``replace``; return the (possibly replaced) root."""
    # The document value is validated by the caller; deep-copy it so the
    # patched document shares no mutable subtree with the request body.
    new_value = copy.deepcopy(value)

    if not tokens:
        # RFC 6902: add/replace at "" replaces the whole document.
        return new_value

    parent_tokens, last = tokens[:-1], str(tokens[-1])
    try:
        parent = pointers.resolve(doc, parent_tokens)
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError(
            PATH_NOT_FOUND, f"parent path does not exist: {exc}", loc=loc
        ) from exc

    if isinstance(parent, dict):
        if op == "replace" and last not in parent:
            raise ApiError(
                PATH_NOT_FOUND,
                f"cannot replace missing object member {last!r} (use 'add' "
                "to create it)",
                loc=loc,
                detail={"member": last},
            )
        parent[last] = new_value
        return doc

    if isinstance(parent, list):
        idx = _array_index(
            last, len(parent), append=(op == "add"), loc=loc
        )
        if op == "add":
            parent.insert(idx, new_value)
        else:
            parent[idx] = new_value
        return doc

    raise ApiError(
        PATH_NOT_FOUND,
        f"parent of {last!r} is a scalar "
        f"({type(parent).__name__}), not an object or array",
        loc=loc,
    )


def _apply_remove(doc: Any, tokens: Tuple[str | int, ...], loc: str) -> Any:
    if not tokens:
        raise ApiError(
            PATH_NOT_FOUND,
            "the root document cannot be removed (path '' has no parent "
            "container); use 'replace' to replace it",
            loc=loc,
        )

    parent_tokens, last = tokens[:-1], str(tokens[-1])
    try:
        parent = pointers.resolve(doc, parent_tokens)
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError(
            PATH_NOT_FOUND, f"parent path does not exist: {exc}", loc=loc
        ) from exc

    if isinstance(parent, dict):
        if last not in parent:
            raise ApiError(
                PATH_NOT_FOUND,
                f"cannot remove missing object member {last!r}",
                loc=loc,
                detail={"member": last},
            )
        del parent[last]
        return doc

    if isinstance(parent, list):
        idx = _array_index(last, len(parent), append=False, loc=loc)
        parent.pop(idx)
        return doc

    raise ApiError(
        PATH_NOT_FOUND,
        f"parent of {last!r} is a scalar ({type(parent).__name__}), not an "
        "object or array",
        loc=loc,
    )


def apply_patch(
    document: Any, raw_ops: Any
) -> Tuple[Any, List[str]]:
    """Validate *raw_ops* fully, then apply them to a copy of *document*.

    Returns ``(patched_document, changed_paths)`` where ``changed_paths``
    contains the resolved RFC 6901 pointer of each successful operation
    (``-`` expanded to the concrete insertion index), de-duplicated and
    sorted by pointer.  Raises :class:`~app.errors.ApiError` located at
    ``operations[i]`` on the first invalid step; no partial result escapes.
    """
    operations = _static_check(raw_ops)

    patched = copy.deepcopy(document)
    changed: set[str] = set()
    for i, operation in enumerate(operations):
        loc = f"operations[{i}]"
        tokens = operation["tokens"]
        inserted_at: int | None = None
        if (
            operation["op"] == "add"
            and tokens
            and str(tokens[-1]) == "-"
        ):
            # Resolve the array now (before insertion) so the reported path
            # names the concrete new index rather than the '-' token.
            try:
                parent = pointers.resolve(patched, tokens[:-1])
            except (KeyError, IndexError, TypeError) as exc:
                raise ApiError(
                    PATH_NOT_FOUND,
                    f"parent path does not exist: {exc}",
                    loc=loc,
                ) from exc
            if isinstance(parent, list):
                inserted_at = len(parent)
            # An object member named '-' is a normal add; a scalar parent is
            # reported by _apply_add_replace with the standard wording.

        if operation["op"] == "remove":
            patched = _apply_remove(patched, tokens, loc)
            changed_path = operation["path"]
        else:
            patched = _apply_add_replace(
                patched,
                tokens,
                operation["value"],
                op=operation["op"],
                loc=loc,
            )
            if inserted_at is not None:
                changed_path = operation["path"][:-1] + str(inserted_at)
            else:
                changed_path = operation["path"]
        changed.add(changed_path)

    return patched, sorted(changed)
