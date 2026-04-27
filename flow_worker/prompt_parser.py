from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REFERENCE_TOKEN_RE = re.compile(r"@((?:[SsVv]\d+)|(?:[1-5]))(?![A-Za-z0-9_])")


@dataclass
class PromptBlock:
    number: int
    tag: str
    body: str
    rendered_prompt: str
    raw: str
    references: list[str]


def _normalize_body(body: str) -> str:
    return "\n".join(line.rstrip() for line in str(body or "").splitlines()).strip()


def _render_prompt(prefix: str, number: int, pad_width: int, body: str) -> tuple[str, str]:
    tag = f"{prefix}{str(number).zfill(max(3, int(pad_width or 3)))}"
    body_text = _normalize_body(body)
    return tag, f"{tag} Prompt : {body_text}"


def parse_prompt_blocks(
    raw_text: str,
    *,
    prefix: str = "S",
    pad_width: int = 3,
    separator: str = "|||",
    extra_prefixes: Iterable[str] = (),
) -> list[PromptBlock]:
    aliases = [str(prefix or "S").strip().upper()]
    for token in extra_prefixes or ():
        norm = str(token or "").strip().upper()
        if norm and norm not in aliases:
            aliases.append(norm)
    prefix_pattern = "|".join(re.escape(item) for item in aliases)
    chunks = [part.strip() for part in str(raw_text or "").split(separator) if part.strip()]
    items: list[PromptBlock] = []
    for chunk in chunks:
        lines = [line.rstrip() for line in chunk.splitlines()]
        if not lines:
            continue
        first = lines[0].strip()
        rest = lines[1:]
        number = None
        body = ""
        tag = ""
        rendered = chunk.strip()

        labeled = re.match(
            rf"^\s*(((?:{prefix_pattern})\s*0*([1-9][0-9]*))(?:\s*>\s*(?:{prefix_pattern})\s*0*([1-9][0-9]*))?)\s*(?:PROMPT|프롬프트)\s*:\s*(.*)\s*$",
            first,
            re.IGNORECASE | re.DOTALL,
        )
        if labeled:
            number = int(labeled.group(3))
            spec = str(labeled.group(1) or "").strip().upper()
            inline = str(labeled.group(5) or "").strip()
            body = _normalize_body("\n".join(part for part in [inline, "\n".join(rest).strip()] if part))
            head = spec.split(">", 1)[0]
            prefix_match = re.match(r"^\s*([A-Za-z]+)", head)
            prefix_value = str(prefix_match.group(1) or prefix).upper() if prefix_match else str(prefix).upper()
            tag = f"{prefix_value}{str(number).zfill(max(3, int(pad_width or 3)))}"
            rendered = f"{spec} Prompt : {body}"
        else:
            inline = re.match(r"^\s*0*([1-9][0-9]*)\s*:\s*(.*)\s*$", first, re.DOTALL)
            if inline:
                number = int(inline.group(1))
                inline_body = str(inline.group(2) or "").strip()
                body = _normalize_body("\n".join(part for part in [inline_body, "\n".join(rest).strip()] if part))
                tag, rendered = _render_prompt(prefix, number, pad_width, body)
            else:
                multi = re.match(r"^\s*0*([1-9][0-9]*)\s*:\s*$", first)
                if not multi:
                    continue
                number = int(multi.group(1))
                body = _normalize_body("\n".join(rest))
                tag, rendered = _render_prompt(prefix, number, pad_width, body)

        if not number or not body:
            continue

        refs: list[str] = []
        seen: set[str] = set()
        for match in REFERENCE_TOKEN_RE.finditer(rendered):
            token = str(match.group(1) or "").upper()
            if token and token not in seen:
                refs.append(token)
                seen.add(token)
        items.append(PromptBlock(number=number, tag=tag, body=body, rendered_prompt=rendered, raw=chunk.strip(), references=refs))
    items.sort(key=lambda item: item.number)
    return items


def load_prompt_blocks(
    path: Path,
    *,
    prefix: str = "S",
    pad_width: int = 3,
    separator: str = "|||",
    extra_prefixes: Iterable[str] = (),
) -> list[PromptBlock]:
    if not path.exists():
        return []
    return parse_prompt_blocks(path.read_text(encoding="utf-8"), prefix=prefix, pad_width=pad_width, separator=separator, extra_prefixes=extra_prefixes)


def summarize_prompt_file(
    path: Path,
    *,
    prefix: str = "S",
    pad_width: int = 3,
    separator: str = "|||",
    extra_prefixes: Iterable[str] = (),
) -> str:
    items = load_prompt_blocks(path, prefix=prefix, pad_width=pad_width, separator=separator, extra_prefixes=extra_prefixes)
    if not items:
        return f"{path.name} | 프롬프트 없음"
    preview = ",".join(f"{item.number:03d}" for item in items[:24])
    if len(items) > 24:
        preview += f"... 외 {len(items) - 24}개"
    return f"{path.name} | 총 {len(items)}개 | {preview}"


def compress_numbers(numbers: Iterable[int], prefix: str = "") -> str:
    nums = sorted({int(num) for num in numbers if int(num) > 0})
    if not nums:
        return ""
    parts: list[str] = []
    start = prev = nums[0]
    for value in nums[1:]:
        if value == prev + 1:
            prev = value
            continue
        parts.append(_fmt_range(start, prev, prefix))
        start = prev = value
    parts.append(_fmt_range(start, prev, prefix))
    return ",".join(parts)


def _fmt_range(start: int, end: int, prefix: str) -> str:
    left = f"{prefix}{start:03d}"
    right = f"{prefix}{end:03d}"
    return left if start == end else f"{left}-{right}"
