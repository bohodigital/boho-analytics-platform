"""Bounded, non-executing semantic evidence extraction for JavaScript sources.

This module deliberately recognizes a small static subset.  Anything outside
that subset becomes an unresolved evidence record; target modules are never
imported and expressions are never evaluated.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Mapping


MAX_FILES = 100_000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_OCCURRENCES = 2_000_000
SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mdx"}
ROUTE_HELPERS = frozenset(
    {"route", "routePath", "hrefFor", "toRoute", "withTrailingSlash", "createRoute", "internalHref"}
)

_IDENTIFIER = r"[A-Za-z_$][\w$]*"
_IMPORT = re.compile(
    rf"import\s*\{{(?P<body>[^}}]{{1,2000}})\}}\s*from\s*"
    r"(?P<quote>['\"])(?P<module>[^'\"]{1,1000})(?P=quote)"
)
_CONST = re.compile(
    rf"(?:export\s+)?const\s+(?P<name>{_IDENTIFIER})\s*(?::[^=;\n]{{1,500}})?=\s*"
    r"(?P<expr>[^;\n]{1,4000})\s*;"
)
_ARRAY = re.compile(
    rf"(?:export\s+)?const\s+(?P<name>{_IDENTIFIER})\s*(?::[^=]{{1,500}})?=\s*"
    r"\[(?P<body>.{0,100000}?)\]\s*;",
    re.DOTALL,
)
_OBJECT = re.compile(r"\{(?P<body>[^{}]{1,10000})\}")
_PROPERTY = re.compile(
    rf"(?P<name>{_IDENTIFIER}|['\"][^'\"]+['\"])\s*:\s*"
    rf"(?P<expr>(?:['\"][^'\"]*['\"])|(?:`[^`]{{0,4000}}`)|"
    rf"(?:{_IDENTIFIER}(?:\.{_IDENTIFIER})?)|(?:{_IDENTIFIER}\([^)]{{0,1000}}\)))"
)
_JSX_ATTR = re.compile(
    r"\b(?P<attr>href|to|url|path|action|formAction)\s*=\s*"
    r"(?:(?P<quote>['\"])(?P<literal>[^'\"]*)(?P=quote)|\{\s*(?P<expr>[^}\n]{1,1000})\s*\})"
)
_ROUTER = re.compile(
    r"\b(?P<call>(?:router\s*\.\s*)?(?:push|replace|prefetch|navigate|redirect|permanentRedirect))"
    r"\s*\(\s*(?P<expr>[^,\)\n]{1,1000})"
)
_SPREAD = re.compile(rf"\{{\s*\.\.\.\s*(?P<expr>{_IDENTIFIER}(?:\.{_IDENTIFIER})?)\s*\}}")
_MAP = re.compile(
    rf"\b(?P<array>{_IDENTIFIER})\s*(?:\.\s*filter\s*\(.{{0,1000}}?\)\s*)?\."
    rf"\s*(?:map|flatMap)\s*\(\s*\(?\s*(?P<item>{_IDENTIFIER})"
)
_EXPLICIT_LAYER = re.compile(r"data-link-layer\s*=\s*['\"](?P<layer>[a-z-]{1,40})['\"]")


class SourceSemanticError(ValueError):
    """Input exceeded a security or determinism bound."""


@dataclass(frozen=True)
class SemanticEvidence:
    evidence_id: str
    source_path: str
    source_line: int
    source_column: int
    source_route: str | None
    raw_destination_expression: str
    destination: str | None
    resolution_state: str
    resolution_kind: str
    component_or_call: str
    layer: str
    layer_evidence: str
    confidence: float
    symbol_provenance: tuple[str, ...]
    unresolved_reason: str | None


@dataclass(frozen=True)
class SemanticAdapterResult:
    """Lane-local output designed for later conversion to ``AdapterResult``."""

    adapter: str
    adapter_version: str
    repository_revision: str
    evidence: tuple[SemanticEvidence, ...]
    coverage: dict[str, int | str]
    diagnostics: tuple[str, ...]
    evidence_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "repository_revision": self.repository_revision,
            "evidence": [asdict(item) for item in self.evidence],
            "coverage": dict(self.coverage),
            "diagnostics": list(self.diagnostics),
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class _Value:
    value: str
    kind: str
    provenance: tuple[str, ...]


def _safe_path(raw: str) -> str:
    path = raw.replace("\\", "/")
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\x00" in path:
        raise SourceSemanticError("source path is unsafe")
    return str(pure)


def _source_route(path: str) -> str | None:
    pure = PurePosixPath(path)
    parts = pure.parts
    if len(parts) >= 2 and parts[0] == "app" and pure.name in {"page.js", "page.jsx", "page.ts", "page.tsx"}:
        route = "/" + "/".join(parts[1:-1])
        return "/" if route == "/" else route.rstrip("/") + "/"
    if len(parts) >= 2 and parts[0] == "pages" and pure.suffix in SOURCE_SUFFIXES:
        tail = list(parts[1:])
        tail[-1] = pure.stem
        if tail[-1] == "index":
            tail.pop()
        return "/" + "/".join(tail) + ("/" if tail else "")
    return None


def _split_top_level(text: str, separator: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    quote = ""
    depth = 0
    for index, character in enumerate(text):
        if quote:
            if character == quote and (index == 0 or text[index - 1] != "\\"):
                quote = ""
            continue
        if character in "'\"`":
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif character == separator and depth == 0:
            pieces.append(text[start:index].strip())
            start = index + 1
    pieces.append(text[start:].strip())
    return pieces


def _literal(expr: str) -> str | None:
    expr = expr.strip()
    if len(expr) < 2 or expr[0] not in "'\"" or expr[-1] != expr[0]:
        return None
    body = expr[1:-1]
    if "\\" in body or any(ord(char) < 32 for char in body):
        return None
    return body


def _resolve(
    expression: str,
    symbols: Mapping[str, _Value],
    *,
    trail: tuple[str, ...] = (),
    depth: int = 0,
) -> tuple[_Value | None, str]:
    expr = expression.strip()
    if depth > 12:
        return None, "resolution-depth-limit"
    literal = _literal(expr)
    if literal is not None:
        return _Value(literal, "literal", trail), ""
    if len(expr) >= 2 and expr[0] == "`" and expr[-1] == "`":
        body = expr[1:-1]
        output: list[str] = []
        cursor = 0
        for match in re.finditer(r"\$\{([^{}]{1,500})\}", body):
            output.append(body[cursor:match.start()])
            value, reason = _resolve(match.group(1), symbols, trail=trail, depth=depth + 1)
            if value is None:
                return None, f"template-{reason}"
            output.append(value.value)
            trail += value.provenance
            cursor = match.end()
        output.append(body[cursor:])
        joined = "".join(output)
        if "${" in joined or any(ord(char) < 32 for char in joined):
            return None, "unsupported-template-expression"
        return _Value(joined, "template", tuple(dict.fromkeys(trail))), ""
    parts = _split_top_level(expr, "+")
    if len(parts) > 1:
        resolved: list[_Value] = []
        for part in parts:
            value, reason = _resolve(part, symbols, trail=trail, depth=depth + 1)
            if value is None:
                return None, f"concatenation-{reason}"
            resolved.append(value)
        return _Value(
            "".join(item.value for item in resolved),
            "concatenation",
            tuple(dict.fromkeys(item for value in resolved for item in value.provenance)),
        ), ""
    call = re.fullmatch(rf"(?P<helper>{_IDENTIFIER})\s*\((?P<args>.{{0,2000}})\)", expr)
    if call:
        helper = call.group("helper")
        if helper not in ROUTE_HELPERS:
            if "env" in helper.lower():
                return None, "environment-expression"
            return None, "runtime-call"
        args = _split_top_level(call.group("args"), ",")
        if not args:
            return None, "route-helper-missing-argument"
        value, reason = _resolve(args[0], symbols, trail=trail, depth=depth + 1)
        if value is None:
            return None, f"route-helper-{reason}"
        destination = value.value
        if not destination.startswith(("/", "#", "http://", "https://", "mailto:", "tel:")):
            destination = "/" + destination.strip("/") + "/"
        return _Value(destination, "route-helper", value.provenance), ""
    value = symbols.get(expr)
    if value is not None:
        if expr in trail:
            return None, "symbol-cycle"
        return _Value(value.value, value.kind, tuple(dict.fromkeys((*trail, *value.provenance)))), ""
    lowered = expr.lower()
    if any(token in lowered for token in ("process.env", "import.meta.env", "window.", "document.", "localstorage")):
        return None, "environment-expression"
    if "?" in expr or "&&" in expr or "||" in expr:
        return None, "conditional-expression"
    if any(
        token in lowered
        for token in ("state", "props", "params", "searchparams", "pathname", "location", "runtime")
    ):
        return None, "state-or-runtime-expression"
    return None, "unsupported-expression"


def _properties(body: str, symbols: Mapping[str, _Value]) -> dict[str, _Value]:
    properties: dict[str, _Value] = {}
    for match in _PROPERTY.finditer(body):
        name = match.group("name").strip("'\"")
        value, _reason = _resolve(match.group("expr"), symbols)
        if value is not None:
            properties.setdefault(name, value)
    return properties


def _layer(line: str, attr: str) -> tuple[str, str, float]:
    explicit = _EXPLICIT_LAYER.search(line)
    if explicit:
        return explicit.group("layer"), "explicit:data-link-layer", 0.98
    lowered = line.lower()
    if attr in {"action", "formAction"} or "onclick" in lowered:
        return "action", "semantic:action", 0.88
    if "<nav" in lowered or "<header" in lowered:
        return "menu", "landmark:nav-or-header", 0.86
    if "<footer" in lowered:
        return "utility", "landmark:footer", 0.84
    return "contextual", "default:source-context", 0.72


def _bounded_map_contexts(
    text: str, arrays: Mapping[str, list[dict[str, _Value]]]
) -> dict[int, dict[str, list[dict[str, _Value]]]]:
    """Return map variables active on each line of a bracketed callback."""

    contexts: dict[int, dict[str, list[dict[str, _Value]]]] = {}
    for match in _MAP.finditer(text):
        items = arrays.get(match.group("array"))
        if not items:
            continue
        arrow = text.find("=>", match.end(), min(len(text), match.end() + 500))
        if arrow < 0:
            continue
        cursor = arrow + 2
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] not in "({":
            line_number = text.count("\n", 0, match.start()) + 1
            contexts.setdefault(line_number, {})[match.group("item")] = items
            continue
        opening = text[cursor]
        closing = ")" if opening == "(" else "}"
        depth = 0
        quote = ""
        end = min(len(text), cursor + 100_000)
        for index in range(cursor, end):
            character = text[index]
            if quote:
                if character == quote and text[index - 1] != "\\":
                    quote = ""
                continue
            if character in "'\"`":
                quote = character
            elif character == opening:
                depth += 1
            elif character == closing:
                depth -= 1
                if depth == 0:
                    start_line = text.count("\n", 0, match.start()) + 1
                    end_line = text.count("\n", 0, index) + 1
                    for line_number in range(start_line, end_line + 1):
                        contexts.setdefault(line_number, {})[match.group("item")] = items
                    break
    return contexts


def _hash_evidence(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _import_matches(importer: str, module: str, declaration_path: str) -> bool:
    if not module.startswith("."):
        return False
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), module))
    candidates = {
        base,
        *(base + suffix for suffix in SOURCE_SUFFIXES),
        *(base + "/index" + suffix for suffix in SOURCE_SUFFIXES),
    }
    return declaration_path in candidates


def extract_source_semantic_evidence(
    sources: Mapping[str, str], *, repository_revision: str = ""
) -> SemanticAdapterResult:
    """Extract deterministic link evidence without executing any source code."""

    if repository_revision and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", repository_revision):
        raise SourceSemanticError("repository revision must be a lowercase Git object ID")
    if len(sources) > MAX_FILES:
        raise SourceSemanticError("source file count exceeds limit")
    if any(not isinstance(path, str) for path in sources):
        raise SourceSemanticError("source paths must be text")
    normalized: dict[str, str] = {}
    total_bytes = 0
    diagnostics: list[str] = []
    for raw_path, text in sorted(sources.items()):
        path = _safe_path(raw_path)
        if path in normalized:
            raise SourceSemanticError("source paths collide after normalization")
        if not isinstance(text, str):
            raise SourceSemanticError("source content must be text")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise SourceSemanticError(f"source file exceeds limit: {path}")
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise SourceSemanticError("total source bytes exceed limit")
        if PurePosixPath(path).suffix.lower() in SOURCE_SUFFIXES:
            normalized[path] = text

    symbols: dict[str, _Value] = {}
    objects: dict[str, dict[str, _Value]] = {}
    arrays: dict[str, list[dict[str, _Value]]] = {}
    array_origins: dict[str, str] = {}
    object_origins: dict[str, str] = {}
    declarations: list[tuple[str, str, str]] = []
    for path, text in sorted(normalized.items()):
        for match in _CONST.finditer(text):
            declarations.append((path, match.group("name"), match.group("expr")))
    ambiguous_symbols = {
        name for name in {item[1] for item in declarations}
        if sum(item[1] == name for item in declarations) > 1
    }
    array_name_counts: dict[str, int] = {}
    for text in normalized.values():
        for match in _ARRAY.finditer(text):
            name = match.group("name")
            array_name_counts[name] = array_name_counts.get(name, 0) + 1
    for _pass in range(12):
        changed = False
        for path, name, expression in declarations:
            if name in symbols or name in ambiguous_symbols:
                continue
            value, _reason = _resolve(expression, symbols)
            if value is not None:
                symbols[name] = _Value(value.value, value.kind, (f"{path}:{name}", *value.provenance))
                changed = True
        if not changed:
            break
    for path, text in sorted(normalized.items()):
        for match in _ARRAY.finditer(text):
            items = [_properties(item.group("body"), symbols) for item in _OBJECT.finditer(match.group("body"))]
            name = match.group("name")
            if array_name_counts[name] == 1:
                arrays[name] = [item for item in items if item]
                array_origins[name] = path
        for match in _CONST.finditer(text):
            expression = match.group("expr").strip()
            if (
                match.group("name") not in ambiguous_symbols
                and expression.startswith("{")
                and expression.endswith("}")
            ):
                name = match.group("name")
                if name not in objects:
                    objects[name] = _properties(expression[1:-1], symbols)
                    object_origins[name] = path
                elif object_origins.get(name) != path:
                    objects.pop(name, None)
                    object_origins.pop(name, None)

    # Named imports are aliases only when the exported identifier is uniquely
    # statically known. Module resolution is intentionally not performed.
    for path, text in sorted(normalized.items()):
        for match in _IMPORT.finditer(text):
            module = match.group("module")
            for item in _split_top_level(match.group("body"), ","):
                parts = re.split(r"\s+as\s+", item.strip(), maxsplit=1)
                source_name, alias = parts[0], parts[-1]
                if source_name in symbols:
                    source = symbols[source_name]
                    declaration = source.provenance[0].rsplit(":", 1)[0]
                    if _import_matches(path, module, declaration):
                        symbols.setdefault(
                            alias,
                            _Value(
                                source.value, "import-alias",
                                (f"{path}:import:{module}:{source_name}", *source.provenance),
                            ),
                        )
                if source_name in arrays and _import_matches(
                    path, module, array_origins[source_name]
                ):
                    arrays.setdefault(alias, arrays[source_name])
                if source_name in objects and _import_matches(
                    path, module, object_origins[source_name]
                ):
                    objects.setdefault(alias, objects[source_name])
    # Object properties may depend on an import alias established above.
    for _path, text in sorted(normalized.items()):
        for match in _CONST.finditer(text):
            expression = match.group("expr").strip()
            if (
                match.group("name") not in ambiguous_symbols
                and expression.startswith("{")
                and expression.endswith("}")
            ):
                resolved_properties = _properties(expression[1:-1], symbols)
                if resolved_properties:
                    objects[match.group("name")] = resolved_properties

    evidence: list[SemanticEvidence] = []

    def append(
        *, path: str, line_number: int, column: int, expression: str, value: _Value | None,
        reason: str, kind: str, component: str, attr: str, confidence_adjustment: float = 0.0,
    ) -> None:
        if len(evidence) >= MAX_OCCURRENCES:
            raise SourceSemanticError("semantic occurrence count exceeds limit")
        layer, layer_evidence, confidence = _layer(lines[line_number - 1], attr)
        destination = value.value if value else None
        if destination and destination.startswith("#"):
            resolution_state = "fragment"
        elif destination and destination.lower().startswith(("mailto:", "tel:", "javascript:")):
            resolution_state = "action"
            layer, layer_evidence = "action", "scheme:action"
        elif destination and destination.lower().startswith(("http://", "https://", "//")):
            resolution_state = "external"
        else:
            resolution_state = "source-only" if value else "unresolved"
        payload: dict[str, object] = {
            "source_path": path,
            "source_line": line_number,
            "source_column": column,
            "source_route": _source_route(path),
            "raw_destination_expression": expression.strip(),
            "destination": destination,
            "resolution_state": resolution_state,
            "resolution_kind": value.kind if value else kind,
            "component_or_call": component,
            "layer": layer,
            "layer_evidence": layer_evidence,
            "confidence": round(max(0.0, confidence + confidence_adjustment), 2),
            "symbol_provenance": value.provenance if value else (),
            "unresolved_reason": None if value else reason,
        }
        evidence_id = _hash_evidence(payload)[:24]
        evidence.append(SemanticEvidence(evidence_id=evidence_id, **payload))  # type: ignore[arg-type]

    for path, text in sorted(normalized.items()):
        lines = text.splitlines()
        bounded_contexts = _bounded_map_contexts(text, arrays)
        for line_number, line in enumerate(lines, 1):
            map_context = dict(bounded_contexts.get(line_number, {}))
            for match in _MAP.finditer(line):
                if match.group("array") in arrays:
                    map_context[match.group("item")] = arrays[match.group("array")]
            for match in _JSX_ATTR.finditer(line):
                attr = match.group("attr")
                expression = (
                    repr(match.group("literal")) if match.group("literal") is not None else match.group("expr")
                )
                assert expression is not None
                mapped = re.fullmatch(rf"(?P<item>{_IDENTIFIER})\.(?P<prop>{_IDENTIFIER})", expression.strip())
                if mapped and mapped.group("item") in map_context:
                    found = False
                    for index, item in enumerate(map_context[mapped.group("item")]):
                        value = item.get(mapped.group("prop"))
                        if value is not None:
                            found = True
                            mapped_value = _Value(
                                value.value, "bounded-map-property",
                                (*value.provenance, f"{path}:{mapped.group('item')}[{index}].{mapped.group('prop')}"),
                            )
                            append(
                                path=path, line_number=line_number, column=match.start() + 1,
                                expression=expression, value=mapped_value, reason="", kind="bounded-map",
                                component="jsx-attribute", attr=attr, confidence_adjustment=0.08,
                            )
                    if found:
                        continue
                value, reason = _resolve(expression, symbols)
                append(
                    path=path, line_number=line_number, column=match.start() + 1,
                    expression=expression, value=value, reason=reason, kind="jsx-expression",
                    component="jsx-attribute", attr=attr,
                )
            for match in _ROUTER.finditer(line):
                expression = match.group("expr")
                value, reason = _resolve(expression, symbols)
                append(
                    path=path, line_number=line_number, column=match.start() + 1,
                    expression=expression, value=value, reason=reason, kind="router-expression",
                    component=match.group("call").replace(" ", ""), attr="action",
                )
            for match in _SPREAD.finditer(line):
                expression = match.group("expr")
                props = objects.get(expression)
                value = props.get("href") if props else None
                value = value or (props.get("to") if props else None)
                append(
                    path=path, line_number=line_number, column=match.start() + 1,
                    expression=f"{expression}.href", value=value,
                    reason="spread-route-property-unresolved" if value is None else "",
                    kind="spread-props", component="jsx-spread", attr="href",
                    confidence_adjustment=0.04,
                )

    evidence.sort(
        key=lambda item: (
            item.source_path, item.source_line, item.source_column,
            item.raw_destination_expression, item.destination or "", item.evidence_id,
        )
    )
    resolved = sum(item.resolution_state != "unresolved" for item in evidence)
    unresolved = len(evidence) - resolved
    coverage: dict[str, int | str] = {
        "classification": "bounded-source-semantic",
        "files_considered": len(normalized),
        "bytes_considered": total_bytes,
        "occurrences": len(evidence),
        "resolved": resolved,
        "unresolved": unresolved,
    }
    canonical = {
        "adapter": "source-semantic",
        "adapter_version": "2.1",
        "repository_revision": repository_revision,
        "evidence": [asdict(item) for item in evidence],
        "coverage": coverage,
        "diagnostics": diagnostics,
    }
    return SemanticAdapterResult(
        adapter="source-semantic",
        adapter_version="2.1",
        repository_revision=repository_revision,
        evidence=tuple(evidence),
        coverage=coverage,
        diagnostics=tuple(diagnostics),
        evidence_hash=_hash_evidence(canonical),
    )
