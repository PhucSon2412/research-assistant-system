"""Deterministic Markdown spans in UTF-8 decoded original code-point offsets.

The small line parser is intentional: heading/bibliography detection can be
checked against the original without a renderer normalising whitespace.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass

PIPELINE_VERSION = "markdown-lines-v1"
HEADING = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$")
REFERENCES = re.compile(r"^(references|bibliography|works cited|tài liệu tham khảo)$", re.I)
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Span:
    kind: str
    heading: str | None
    ordinal: int
    text: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Candidate:
    field_name: str
    value: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class Extracted:
    sections: list[Span]
    candidates: list[Candidate]
    coverage: str
    warnings: list[str]


def line_starts(original: str) -> list[int]:
    return [0] + [i + 1 for i, char in enumerate(original) if char == "\n"]


def line_at(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def extract(original: str) -> Extracted:
    starts = line_starts(original)
    lines = original.splitlines(keepends=True)
    boundaries: list[tuple[int, str, str | None]] = [(0, "body", None)]
    candidates: list[Candidate] = []
    fence_char: str | None = None
    fence_length = 0
    for line_no, line in enumerate(lines):
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char = None
            continue
        if fence_char is not None:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        heading = match.group(2).strip()
        kind = "references" if REFERENCES.fullmatch(heading) else "abstract" if heading.casefold() == "abstract" else "body"
        offset = starts[line_no]
        if offset == 0:
            boundaries[0] = (0, kind, heading)
        else:
            boundaries.append((offset, kind, heading))
        if match.group(1) == "#" and not candidates:
            candidates.append(Candidate("title", heading, offset + match.start(2), offset + match.end(2)))
    sections: list[Span] = []
    for index, (start, kind, heading) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(original)
        if not original[start:end].strip():
            continue
        sections.append(Span(kind, heading, len(sections), original[start:end], start, end,
                             line_at(starts, start), line_at(starts, end - 1)))
    return Extracted(sections, candidates, "complete" if sections else "none", [] if sections else ["no_text"])


def has_body_text(result: Extracted) -> bool:
    """A title, abstract or bibliography alone is not a paper body."""
    return any(any(char.isalnum() for char in line)
               for section in result.sections if section.kind == "body"
               for line in section.text.splitlines() if not HEADING.match(line))


def chunk_section(original: str, section: Span, max_chars: int) -> list[Span]:
    starts = line_starts(original)
    result: list[Span] = []
    cursor = section.start_offset
    while cursor < section.end_offset:
        while cursor < section.end_offset and original[cursor].isspace():
            cursor += 1
        if cursor >= section.end_offset:
            break
        limit = min(cursor + max_chars, section.end_offset)
        end = limit
        if limit < section.end_offset:
            split = max(original.rfind(" ", cursor + max_chars // 2, limit),
                        original.rfind("\n", cursor + max_chars // 2, limit))
            if split > cursor:
                end = split
        while end > cursor and original[end - 1].isspace():
            end -= 1
        if end <= cursor:
            end = limit
        result.append(Span(section.kind, section.heading, len(result), original[cursor:end], cursor, end,
                           line_at(starts, cursor), line_at(starts, end - 1)))
        cursor = end
    return result


def chunk_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
