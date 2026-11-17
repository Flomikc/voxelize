"""Проверка запроса от Java: все пути обязаны лежать внутри {data_dir}/tasks/{task_id}/."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.config import Settings
from app.schemas import GenerateRequest, Job

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png"}


def build_job(req: GenerateRequest, cfg: Settings) -> Job:
    tasks_root = (cfg.data_dir / "tasks").resolve()
    task_dir = (tasks_root / req.task_id).resolve()
    if not task_dir.is_relative_to(tasks_root) or not task_dir.is_dir():
        raise HTTPException(400, "Каталог задачи не найден в общем томе")

    if len(req.input_files) > cfg.max_input_files:
        raise HTTPException(400, f"Слишком много файлов (максимум {cfg.max_input_files})")

    input_dir = (task_dir / "input").resolve()
    files: list[Path] = []
    for raw in req.input_files:
        p = Path(raw).resolve()
        if not p.is_relative_to(input_dir):
            raise HTTPException(400, "Путь к изображению выходит за пределы каталога задачи")
        if p.suffix.lower() not in ALLOWED_SUFFIXES or not p.is_file():
            raise HTTPException(400, f"Файл не найден или не является JPG/PNG: {p.name}")
        files.append(p)

    work = Path(req.work_dir).resolve()
    out = Path(req.output_path).resolve()
    if not work.is_relative_to(task_dir) or not out.is_relative_to(task_dir) or out.suffix != ".glb":
        raise HTTPException(400, "work_dir/output_path должны лежать внутри каталога задачи")

    return Job(task_id=req.task_id, input_files=sorted(files), work_dir=work, output_path=out,
               options=req.options)
