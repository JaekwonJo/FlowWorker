from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QueueItem:
    number: int
    tag: str
    prompt: str
    status: str = "pending"
    message: str = ""
    file_name: str = ""
