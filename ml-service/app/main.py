"""FastAPI-микросервис генерации 3D. Контракт: POST /generate (202), GET /status/{id}, GET /health."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.runner import shutdown_executor, submit_job
from app.schemas import AcceptedResponse, GenerateRequest, HealthResponse, StatusResponse
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
