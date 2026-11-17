"""FastAPI-микросервис генерации 3D. Контракт: POST /generate (202), GET /status/{id}, GET /health."""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.errors import PipelineError
from app.pipeline import postprocess
from app.runner import shutdown_executor, submit_job
from app.schemas import (AcceptedResponse, GenerateRequest, HealthResponse, ProcessRequest,
                         ProcessResponse, StatusResponse)
from app.utils.progress import TaskRegistry, is_valid_task_id
from app.validation import build_job

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ml-service")

settings = get_settings()
registry = TaskRegistry(settings.data_dir / "tasks")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("ml-service запущен. data_dir=%s device=%s threads=%d",
             settings.data_dir, settings.device, settings.num_threads)
    yield
    shutdown_executor()


app = FastAPI(title="Photo-to-3D ML service", version="1.0.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health():
    # Лёгкая проверка (без импорта torch): общий том смонтирован и доступен на запись.
    tasks_dir = settings.data_dir / "tasks"
    if not (tasks_dir.is_dir() and os.access(tasks_dir, os.W_OK)):
        raise HTTPException(503, "Общий том недоступен")
    return HealthResponse(status="ok", device_setting=settings.device, data_dir=str(settings.data_dir))


@app.post("/generate", status_code=202, response_model=AcceptedResponse)
def generate(req: GenerateRequest):
    job = build_job(req, settings)
    if not registry.register(job.task_id):             # повторный POST (ретрай Java) — не дублируем работу
        snap = registry.get(job.task_id) or {"status": "QUEUED"}
        return AcceptedResponse(task_id=job.task_id, status=snap["status"])
    submit_job(job, registry)
    return AcceptedResponse(task_id=job.task_id, status="QUEUED")


@app.get("/status/{task_id}", response_model=StatusResponse)
def status(task_id: str):
    if not is_valid_task_id(task_id):
        raise HTTPException(404, "Задача не найдена")
    snap = registry.get(task_id)
    if snap is None:
        raise HTTPException(404, "Задача не найдена")
    return StatusResponse(**snap)


# ───────────────────────── пост-обработка готовой модели ─────────────────────────
_post_gate = threading.Semaphore(1)   # одно сглаживание за раз: ползунок не должен «душить» генерацию


@app.post("/process/{task_id}", response_model=ProcessResponse)
def process(task_id: str, req: ProcessRequest):
    """Сглаживание Taubin и/или экспорт STL для уже готовой модели. Результат кэшируется по параметрам."""
    if not is_valid_task_id(task_id):
        raise HTTPException(404, "Задача не найдена")
    snap = registry.get(task_id)
    if snap is None:
        raise HTTPException(404, "Задача не найдена")
    if snap["status"] != "DONE":
        raise HTTPException(409, "Модель ещё не готова")

    post_dir = settings.data_dir / "tasks" / task_id / "post"
    try:
        with _post_gate:
            path = postprocess.build_variant(post_dir, req.smoothing, req.format, req.size_mm)
    except PipelineError as e:
        raise HTTPException(409 if "нет данных" in str(e) else 422, str(e))
    meta = json.loads((post_dir / "meta.json").read_text(encoding="utf-8"))
    return ProcessResponse(task_id=task_id, smoothing=req.smoothing, format=req.format,
                           relative_path=f"post/variants/{path.name}", size_bytes=path.stat().st_size,
                           vertices=meta["vertices"], faces=meta["faces"])
