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
    media_mode: str = "image"
    prompt_head: str = ""
    route_start_tag: str = ""
    route_end_tag: str = ""
    frame_start_tag: str = ""
    frame_end_tag: str = ""


def _normalize_body(body: str) -> str:
    return "\n".join(line.rstrip() for line in str(body or "").splitlines()).strip()


def _render_prompt(prefix: str, number: int, pad_width: int, body: str) -> tuple[str, str]:
    tag = f"{prefix}{str(number).zfill(max(3, int(pad_width or 3)))}"
    body_text = _normalize_body(body)
    return tag, f"{tag} Prompt : {body_text}"


def _normalize_tag(raw: str, default_prefix: str, pad_width: int) -> str:
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    match = re.match(r"^\s*([A-Z]+)?\s*0*([1-9][0-9]*)\s*$", text, re.IGNORECASE)
    if not match:
        return ""
    prefix = str(match.group(1) or default_prefix or "S").strip().upper()
    number = int(match.group(2))
    return f"{prefix}{str(number).zfill(max(3, int(pad_width or 3)))}"


def _route_frame_tags(start_tag: str, end_tag: str) -> tuple[str, str]:
    start = ""
    end = ""
    if start_tag:
        digits = re.sub(r"\D", "", start_tag)
        if digits:
            start = f"S{str(int(digits)).zfill(3)}"
    if end_tag:
        digits = re.sub(r"\D", "", end_tag)
        if digits:
            end = f"S{str(int(digits)).zfill(3)}"
    return start, end


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
        prompt_head = ""
        media_mode = "image"
        route_start_tag = ""
        route_end_tag = ""
        frame_start_tag = ""
        frame_end_tag = ""

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
            prompt_head = tag
            rendered = f"{tag} Prompt : {body}"
            media_mode = "video" if prefix_value == "V" else "image"
            route_start_tag = _normalize_tag(head, prefix_value, pad_width)
            if ">" in spec:
                tail = spec.split(">", 1)[1]
                route_end_tag = _normalize_tag(tail, prefix_value, pad_width)
            frame_start_tag, frame_end_tag = _route_frame_tags(route_start_tag, route_end_tag)
        else:
            inline = re.match(r"^\s*0*([1-9][0-9]*)\s*:\s*(.*)\s*$", first, re.DOTALL)
            if inline:
                number = int(inline.group(1))
                inline_body = str(inline.group(2) or "").strip()
                body = _normalize_body("\n".join(part for part in [inline_body, "\n".join(rest).strip()] if part))
                tag, rendered = _render_prompt(prefix, number, pad_width, body)
                prompt_head = tag
                media_mode = "video" if str(prefix or "S").upper() == "V" else "image"
            else:
                multi = re.match(r"^\s*0*([1-9][0-9]*)\s*:\s*$", first)
                if not multi:
                    continue
                number = int(multi.group(1))
                body = _normalize_body("\n".join(rest))
                tag, rendered = _render_prompt(prefix, number, pad_width, body)
                prompt_head = tag
                media_mode = "video" if str(prefix or "S").upper() == "V" else "image"

        if not number or not body:
            continue

        refs: list[str] = []
        seen: set[str] = set()
        for match in REFERENCE_TOKEN_RE.finditer(rendered):
            token = str(match.group(1) or "").upper()
            if token and token not in seen:
                refs.append(token)
                seen.add(token)
        items.append(
            PromptBlock(
                number=number,
                tag=tag,
                body=body,
                rendered_prompt=rendered,
                raw=chunk.strip(),
                references=refs,
                media_mode=media_mode,
                prompt_head=prompt_head or tag,
                route_start_tag=route_start_tag or tag,
                route_end_tag=route_end_tag,
                frame_start_tag=frame_start_tag,
                frame_end_tag=frame_end_tag,
            )
        )
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
