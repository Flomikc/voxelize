"""Контракт с Java-бэкендом (см. MlGenerateRequest / MlStatusResponse в backend)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class Stage(str, Enum):
    """Имена совпадают с enum TaskStatus на стороне Java."""
    QUEUED = "QUEUED"
    DEPTH_ESTIMATION = "DEPTH_ESTIMATION"
    MESH_GENERATION = "MESH_GENERATION"
    TEXTURING = "TEXTURING"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINAL = {Stage.DONE.value, Stage.FAILED.value}


class GenerateRequest(BaseModel):
    task_id: str = Field(pattern=UUID_PATTERN)
    input_files: list[str] = Field(min_length=1, max_length=100)
    work_dir: str
    output_path: str


class AcceptedResponse(BaseModel):
    task_id: str
    status: str


class StatusResponse(BaseModel):
    status: str
    progress: int
    message: str = ""
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    device_setting: str
    data_dir: str


@dataclass(frozen=True)
class Job:
    """Проверенное задание: все пути гарантированно лежат внутри tasks/{task_id}/."""
    task_id: str
    input_files: list[Path]
    work_dir: Path
    output_path: Path
