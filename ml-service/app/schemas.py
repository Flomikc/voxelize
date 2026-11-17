"""Контракт с Java-бэкендом (см. MlGenerateRequest / MlStatusResponse в backend)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

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


class DetailLevel(str, Enum):
    """Уровень детализации геометрии = разрешение marching cubes в TripoSR."""
    low = "low"
    medium = "medium"
    high = "high"
    ultra = "ultra"


MC_RESOLUTION = {
    DetailLevel.low: 128,
    DetailLevel.medium: 192,
    DetailLevel.high: 256,
    DetailLevel.ultra: 320,
}


class GenerateOptions(BaseModel):
    """Параметры генерации. Все поля необязательны — Java, не знающий про них, продолжает работать."""
    detail_level: DetailLevel = DetailLevel.medium
    polygon_limit: int = Field(default=50_000, description="Максимум граней в итоговом меше; 0 = без лимита")
    use_rembg: bool = True

    @field_validator("polygon_limit")
    @classmethod
    def _polygon_limit_range(cls, v: int) -> int:
        if v != 0 and not (1_000 <= v <= 5_000_000):
            raise ValueError("polygon_limit: 0 (без лимита) или от 1000 до 5 000 000")
        return v


class GenerateRequest(BaseModel):
    task_id: str = Field(pattern=UUID_PATTERN)
    input_files: list[str] = Field(min_length=1, max_length=100)
    work_dir: str
    output_path: str
    options: GenerateOptions = Field(default_factory=GenerateOptions)


class ProcessRequest(BaseModel):
    """POST /process/{task_id}: пост-обработка готовой модели."""
    smoothing: int = Field(default=0, ge=0, le=10, description="Уровень сглаживания Taubin, 0 = без сглаживания")
    format: Literal["glb", "stl"] = "glb"
    size_mm: float = Field(default=100.0, gt=0, le=2000, description="Только для STL: наибольший габарит, мм")


class ProcessResponse(BaseModel):
    task_id: str
    smoothing: int
    format: str
    relative_path: str           # относительно каталога задачи, напр. post/variants/s04.glb
    size_bytes: int
    vertices: int
    faces: int


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
    options: GenerateOptions = field(default_factory=GenerateOptions)
