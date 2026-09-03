"""Safe, deterministic portable-Markdown normalization and splitting."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass

from ricky.notifications.types import MessageTextFormat

_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_IMAGE_RE = re.compile(r"!\[(?P<label>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_REFERENCE_IMAGE_RE = re.compile(r"!\[(?P<label>[^\]]*)\](?:\[[^\]]*\])?")
_LINK_RE = re.compile(
    r"(?<!!)\[(?P<label>[^\]\n]+)\]\(\s*"
    r"(?P<destination><[^>\n]+>|[^)\s]+)"
    r"(?P<title>\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
_REFERENCE_DEFINITION_RE = re.compile(
    r"^(?P<indent> {0,3})\[(?P<label>[^\]\n]+)\]:\s*"
    r"(?P<destination><[^>\n]+>|\S+)"
    r"(?P<title>\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*$"
)
_REFERENCE_LINK_RE = re.compile(r"(?<!!)\[(?P<label>[^\]\n]+)\]\[(?P<reference>[^\]\n]*)\]")
_SHORTCUT_REFERENCE_LINK_RE = re.compile(r"(?<!!)\[(?P<label>[^\]\n]+)\](?![\[(])")
_INLINE_CODE_RE = re.compile(r"(`+)(.*?)(\1)")
_INLINE_FORMAT_RE = re.compile(
    r"(?<!\\)(?P<delimiter>\*\*|__|~~|==|\|\||\$\$|\*|_|\$)"
    r"(?P<body>.+?)(?<!\\)(?P=delimiter)"
)
_PLATFORM_LINK_RE = re.compile(r"(?i)^(?:tg|slack|discord)://")
_RAW_PLATFORM_LINK_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:tg|slack|discord)://[^\s<>()\[\]]+"
    r"|https?://(?:www\.)?(?:t|telegram)\.me(?:/[^\s<>()\[\]]*)?"
    r")"
)
_SAFE_LINK_RE = re.compile(r"(?i)^(?:https?://|mailto:)")
_TELEGRAM_WEB_LINK_RE = re.compile(r"(?i)^https?://(?:www\.)?(?:t|telegram)\.me(?:/|$)")
_PLATFORM_MENTION_RE = re.compile(r"(?<![\w.+/@=?:&#%-])@(?P<name>[A-Za-z][A-Za-z0-9_]{4,31})\b")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_BLOCK_QUOTE_RE = re.compile(r"^(?P<prefix>\s{0,3}(?:>\s?)+)(?P<body>.*)$")
_LIST_PREFIX_RE = re.compile(
    r"^(?P<prefix>\s{0,3}(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?)(?P<body>.*)$"
)
_HEADING_PREFIX_RE = re.compile(r"^(?P<prefix>\s{0,3}#{1,6}\s+)(?P<body>.*)$")
_MARKDOWN_ESCAPE_CHARS = r"\`*_{}[]<>()#+-.!|~"


@dataclass(frozen=True)
class _InlineSpan:
    opener: str
    body: str
    closer: str
    parse_body: bool
    plain_suffix: str = ""


def normalize_portable_markdown(text: str) -> str:
    """Return safe portable Markdown while preserving supported structure.

    Raw HTML, embedded media, and provider-control links are neutralized outside
    inline and fenced code. The output is suitable for a transport renderer; it
    is not a permission boundary for attachments or interactive controls.
    """

    value = text.strip()
    if not value:
        raise ValueError("portable Markdown cannot be empty")
    source_lines = value.splitlines()
    references = _collect_reference_definitions(source_lines)
    lines: list[str] = []
    fence: str | None = None
    for line in source_lines:
        match = _FENCE_RE.match(line)
        if match is not None:
            marker = match.group("fence")
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            lines.append(line.rstrip())
            continue
        lines.append(
            line.rstrip()
            if fence is not None
            else _sanitize_inline_regions(line.rstrip(), references)
        )
    if fence is not None:
        lines.append(fence)
    return "\n".join(lines).strip()


def escape_markdown_text(text: str) -> str:
    """Escape one plain-text value for interpolation into portable Markdown."""

    return "".join(f"\\{char}" if char in _MARKDOWN_ESCAPE_CHARS else char for char in text)


def compose_notification_text(
    *,
    title: str | None,
    body: str,
    text_format: MessageTextFormat,
) -> str:
    """Combine a plain title and formatted body without changing body semantics."""

    if text_format == "plain_text":
        return f"{title}\n\n{body}" if title is not None else body
    normalized = normalize_portable_markdown(body)
    if title is None:
        return normalized
    return f"## {escape_markdown_text(title)}\n\n{normalized}"


def split_message_text(
    text: str,
    *,
    text_format: MessageTextFormat,
    limit: int,
) -> list[str]:
    """Split one message deterministically into independently deliverable parts."""

    if limit < 1:
        raise ValueError("message split limit must be positive")
    if not text:
        raise ValueError("message text cannot be empty")
    if text_format == "plain_text":
        return _split_raw(text, limit)
    normalized = normalize_portable_markdown(text)
    units = _markdown_units(normalized)
    pieces: list[str] = []
    for unit in units:
        pieces.extend(_split_markdown_unit(unit, limit))
    return _pack_units(pieces, limit)


def portable_markdown_to_plain_text(text: str) -> str:
    """Project normalized portable Markdown to readable fallback text."""

    normalized = normalize_portable_markdown(text)
    output: list[str] = []
    in_fence = False
    for line in normalized.splitlines():
        if _FENCE_RE.match(line) is not None:
            in_fence = not in_fence
            continue
        if in_fence:
            output.append(line)
            continue
        line = _HEADING_RE.sub("", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = _LINK_RE.sub(
            lambda match: _plain_link(
                match.group("label"), _link_target(match.group("destination"))
            ),
            line,
        )
        line = re.sub(r"(?<!\\)(?:\*\*|__|~~|==)(.+?)(?<!\\)(?:\*\*|__|~~|==)", r"\1", line)
        line = re.sub(r"(?<!\\)(?:\*|_)(.+?)(?<!\\)(?:\*|_)", r"\1", line)
        line = _INLINE_CODE_RE.sub(lambda match: match.group(2), line)
        line = re.sub(r"\\([\\`*_{}\[\]<>()#+\-.!|~])", r"\1", line)
        output.append(html.unescape(line))
    projected = "\n".join(output).strip()
    return projected or "[message contained no plain-text content]"


def _collect_reference_definitions(
    lines: Iterable[str],
) -> dict[str, tuple[str, str, str]]:
    references: dict[str, tuple[str, str, str]] = {}
    fence: str | None = None
    for line in lines:
        fence_match = _FENCE_RE.match(line)
        if fence_match is not None:
            marker = fence_match.group("fence")
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _REFERENCE_DEFINITION_RE.match(line.rstrip())
        if match is None or match.group("label").startswith("^"):
            continue
        key = _reference_key(match.group("label"))
        if key and key not in references:
            references[key] = (
                match.group("destination"),
                _link_target(match.group("destination")),
                match.group("title") or "",
            )
    return references


def _sanitize_inline_regions(
    line: str,
    references: dict[str, tuple[str, str, str]],
) -> str:
    quote = _BLOCK_QUOTE_RE.match(line)
    prefix = quote.group("prefix") if quote is not None else ""
    if quote is not None:
        line = quote.group("body")
    definition = _REFERENCE_DEFINITION_RE.match(line)
    if definition is not None and not definition.group("label").startswith("^"):
        return prefix
    parts: list[str] = []
    cursor = 0
    for match in _INLINE_CODE_RE.finditer(line):
        parts.append(_sanitize_text(line[cursor : match.start()], references))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_sanitize_text(line[cursor:], references))
    return prefix + "".join(parts)


def _sanitize_text(
    value: str,
    references: dict[str, tuple[str, str, str]],
) -> str:
    value = _IMAGE_RE.sub(_safe_image_text, value)
    value = _REFERENCE_IMAGE_RE.sub(_safe_reference_image_text, value)
    value = _LINK_RE.sub(_safe_link_text, value)
    value = _REFERENCE_LINK_RE.sub(
        lambda match: _safe_reference_link_text(match, references), value
    )
    value = _SHORTCUT_REFERENCE_LINK_RE.sub(
        lambda match: _safe_reference_link_text(match, references), value
    )
    value = _RAW_PLATFORM_LINK_RE.sub("[platform link omitted]", value)
    value = _PLATFORM_MENTION_RE.sub(lambda match: f"@\u200b{match.group('name')}", value)
    return value.replace("<", "&lt;").replace(">", "&gt;")


def _safe_image_text(match: re.Match[str]) -> str:
    label = match.group("label").strip() or "media"
    target = match.group("target")
    if _SAFE_LINK_RE.match(target):
        return f"Image: [{label}]({target})"
    return f"Image: {label}"


def _safe_reference_image_text(match: re.Match[str]) -> str:
    return f"Image: {match.group('label').strip() or 'media'}"


def _safe_link_text(match: re.Match[str]) -> str:
    label = match.group("label")
    target = _link_target(match.group("destination"))
    if _PLATFORM_LINK_RE.match(target) or _TELEGRAM_WEB_LINK_RE.match(target):
        return label
    if _SAFE_LINK_RE.match(target) or target.startswith("#"):
        return match.group(0)
    return f"{label} ({target})"


def _safe_reference_link_text(
    match: re.Match[str],
    references: dict[str, tuple[str, str, str]],
) -> str:
    label = match.group("label")
    reference = match.groupdict().get("reference") or label
    definition = references.get(_reference_key(reference))
    if definition is None:
        return match.group(0)
    destination, target, title = definition
    if _PLATFORM_LINK_RE.match(target) or _TELEGRAM_WEB_LINK_RE.match(target):
        return label
    if _SAFE_LINK_RE.match(target) or target.startswith("#"):
        return f"[{label}]({destination}{title})"
    return f"{label} ({target})"


def _reference_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _link_target(destination: str) -> str:
    if destination.startswith("<") and destination.endswith(">"):
        return destination[1:-1]
    return destination


def _plain_link(label: str, target: str) -> str:
    return label if target.startswith("#") else f"{label} ({target})"


def _markdown_units(text: str) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if match is not None:
            marker = match.group("fence")
            if fence is None:
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            current.append(line)
            if fence is None:
                units.append("\n".join(current).strip())
                current = []
            continue
        if not line.strip() and fence is None:
            if current:
                units.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _split_markdown_unit(unit: str, limit: int) -> list[str]:
    if len(unit) <= limit:
        return [unit]
    lines = unit.splitlines()
    opening = _FENCE_RE.match(lines[0]) if lines else None
    if opening is not None:
        return _split_fenced_block(lines, limit)
    if len(lines) >= 2 and _TABLE_SEPARATOR_RE.match(lines[1]):
        return _split_table(lines, limit)
    return _split_markdown_lines(lines, limit)


def _split_fenced_block(lines: list[str], limit: int) -> list[str]:
    opening = lines[0]
    marker_match = _FENCE_RE.match(opening)
    assert marker_match is not None
    marker = marker_match.group("fence")
    content = lines[1:-1] if len(lines) > 1 and _FENCE_RE.match(lines[-1]) else lines[1:]
    overhead = len(opening) + len(marker) + 2
    available = limit - overhead
    if available < 1:
        raise ValueError("message split limit is too small for a Markdown code fence")
    chunks = _split_lines(content, available) if content else [""]
    return [f"{opening}\n{chunk}\n{marker}" for chunk in chunks]


def _split_table(lines: list[str], limit: int) -> list[str]:
    prefix = "\n".join(lines[:2])
    if len(prefix) > limit:
        return _split_markdown_lines(lines, limit)
    chunks: list[str] = []
    current = prefix
    for row in lines[2:]:
        candidate = f"{current}\n{row}"
        if len(candidate) <= limit:
            current = candidate
            continue
        chunks.append(current)
        if len(prefix) + 1 + len(row) <= limit:
            current = f"{prefix}\n{row}"
        else:
            chunks.extend(_split_markdown_lines([row], limit))
            current = prefix
    if current != prefix or not chunks:
        chunks.append(current)
    return chunks


def _split_lines(lines: Iterable[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        for segment in _split_raw(line, limit):
            candidate = segment if not current else f"{current}\n{segment}"
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = segment
    if current or not chunks:
        chunks.append(current)
    return chunks


def _split_markdown_lines(lines: Iterable[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        for segment in _split_markdown_line(line, limit):
            candidate = segment if not current else f"{current}\n{segment}"
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = segment
    if current or not chunks:
        chunks.append(current)
    return chunks


def _split_markdown_line(line: str, limit: int) -> list[str]:
    prefix = ""
    body = line
    quote = _BLOCK_QUOTE_RE.match(body)
    if quote is not None:
        prefix += quote.group("prefix")
        body = quote.group("body")
    block = _LIST_PREFIX_RE.match(body) or _HEADING_PREFIX_RE.match(body)
    if block is not None:
        prefix += block.group("prefix")
        body = block.group("body")
    if prefix and len(prefix) < limit:
        parts = _split_inline_markdown(body, limit - len(prefix))
        return [f"{prefix}{part}" for part in parts]
    return _split_inline_markdown(line, limit)


def _split_inline_markdown(text: str, limit: int, *, depth: int = 0) -> list[str]:
    if len(text) <= limit:
        return [text]
    if depth >= 16:
        return _split_inline_text(escape_markdown_text(text), limit)
    nodes = _inline_nodes(text)
    pieces: list[str] = []
    for node in nodes:
        pieces.extend(_split_inline_node(node, limit, depth=depth))
    return _pack_inline_pieces(pieces, limit)


def _inline_nodes(text: str) -> list[str | _InlineSpan]:
    nodes: list[str | _InlineSpan] = []
    cursor = 0
    while cursor < len(text):
        candidate = _next_inline_span(text, cursor)
        if candidate is None:
            nodes.append(text[cursor:])
            break
        start, end, span = candidate
        if start > cursor:
            nodes.append(text[cursor:start])
        nodes.append(span)
        cursor = end
    return nodes


def _next_inline_span(
    text: str,
    start: int,
) -> tuple[int, int, _InlineSpan] | None:
    candidates: list[tuple[int, int, int, _InlineSpan]] = []
    link = _LINK_RE.search(text, start)
    if link is not None:
        destination = link.group("destination")
        title = link.group("title") or ""
        candidates.append(
            (
                link.start(),
                0,
                link.end(),
                _InlineSpan(
                    opener="[",
                    body=link.group("label"),
                    closer=f"]({destination}{title})",
                    parse_body=True,
                    plain_suffix=f" ({_link_target(destination)})",
                ),
            )
        )
    code = _INLINE_CODE_RE.search(text, start)
    if code is not None:
        candidates.append(
            (
                code.start(),
                1,
                code.end(),
                _InlineSpan(
                    opener=code.group(1),
                    body=code.group(2),
                    closer=code.group(3),
                    parse_body=False,
                ),
            )
        )
    formatted = _INLINE_FORMAT_RE.search(text, start)
    if formatted is not None:
        delimiter = formatted.group("delimiter")
        candidates.append(
            (
                formatted.start(),
                2,
                formatted.end(),
                _InlineSpan(
                    opener=delimiter,
                    body=formatted.group("body"),
                    closer=delimiter,
                    parse_body=True,
                ),
            )
        )
    if not candidates:
        return None
    position, _, end, span = min(candidates, key=lambda item: (item[0], item[1]))
    return position, end, span


def _split_inline_node(
    node: str | _InlineSpan,
    limit: int,
    *,
    depth: int,
) -> list[str]:
    if isinstance(node, str):
        return _split_inline_text(node, limit)
    overhead = len(node.opener) + len(node.closer)
    if overhead >= limit:
        return _split_degraded_span(node, limit)
    available = limit - overhead
    body_parts = (
        _split_inline_markdown(node.body, available, depth=depth + 1)
        if node.parse_body
        else _split_inline_text(node.body, available)
    )
    if node.parse_body and node.opener != "[":
        return [_wrap_formatted_part(node, part) for part in body_parts]
    return [f"{node.opener}{part}{node.closer}" for part in body_parts]


def _wrap_formatted_part(span: _InlineSpan, part: str) -> str:
    leading = part[: len(part) - len(part.lstrip())]
    without_leading = part[len(leading) :]
    trailing = without_leading[len(without_leading.rstrip()) :]
    body = without_leading[: len(without_leading) - len(trailing)] if trailing else without_leading
    if not body:
        return part
    return f"{leading}{span.opener}{body}{span.closer}{trailing}"


def _split_degraded_span(span: _InlineSpan, limit: int) -> list[str]:
    plain = f"{span.body}{span.plain_suffix}"
    escaped = escape_markdown_text(plain)
    return _split_inline_text(escaped, limit)


def _split_inline_text(text: str, limit: int) -> list[str]:
    if not text:
        return [""]
    atoms: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] == "\\" and cursor + 1 < len(text):
            atoms.append(text[cursor : cursor + 2])
            cursor += 2
            continue
        if text[cursor] == "&":
            entity = re.match(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);", text[cursor:])
            if entity is not None and len(entity.group(0)) <= limit:
                atoms.append(entity.group(0))
                cursor += len(entity.group(0))
                continue
        atoms.append(text[cursor])
        cursor += 1
    chunks: list[str] = []
    current = ""
    for atom in atoms:
        if len(atom) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_raw(atom, limit))
        elif len(current) + len(atom) <= limit:
            current += atom
        else:
            chunks.append(current)
            current = atom
    if current:
        chunks.append(current)
    return chunks or [""]


def _pack_inline_pieces(pieces: Iterable[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            raise ValueError("portable Markdown inline splitter exceeded its limit")
        if len(current) + len(piece) <= limit:
            current += piece
        else:
            chunks.append(current)
            current = piece
    if current or not chunks:
        chunks.append(current)
    return chunks


def _pack_units(units: list[str], limit: int) -> list[str]:
    packed: list[str] = []
    current = ""
    for unit in units:
        candidate = unit if not current else f"{current}\n\n{unit}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                packed.append(current)
            current = unit
    if current:
        packed.append(current)
    if not packed or any(not part or len(part) > limit for part in packed):
        raise ValueError("portable Markdown splitter produced an invalid part")
    return packed


def _split_raw(text: str, limit: int) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]
