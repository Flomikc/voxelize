"""
Очередь задач. ThreadPoolExecutor(max_workers=1) гарантирует, что инференс идёт строго
по одной задаче (FIFO) — это и есть защита от нехватки памяти на слабой машине
(строже семафора: лишние задачи просто ждут в очереди со статусом QUEUED).
"""
from __future__ import annotations

import errno
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.errors import PipelineError
from app.pipeline import router
from app.schemas import Job
from app.utils.memory import configure_torch, free_memory
from app.utils.progress import ProgressReporter, TaskRegistry

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml-worker")


def submit_job(job: Job, registry: TaskRegistry) -> None:
    _executor.submit(_run, job, registry)


def shutdown_executor() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)


def _run(job: Job, registry: TaskRegistry) -> None:
    progress = ProgressReporter(registry, job.task_id)
    cfg = get_settings()
    try:
        configure_torch()
        router.generate(job, progress)

        if not job.output_path.is_file():
            raise PipelineError("Пайплайн завершился, но model.glb не создан")
        if not cfg.keep_work_files:                    # промежуточные данные больше не нужны
            shutil.rmtree(job.work_dir, ignore_errors=True)
        progress.done()
    except PipelineError as e:
        log.error("[%s] Ошибка пайплайна: %s", job.task_id, e)
        progress.fail(str(e))
    except Exception as e:                             # noqa: BLE001 — воркер не должен падать
        log.exception("[%s] Необработанная ошибка", job.task_id)
        progress.fail(_friendly_error(e))
    finally:
        free_memory()


def _friendly_error(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, MemoryError) or any(s in text for s in
                                           ("out of memory", "not enough memory", "can't allocate memory")):
        return ("Недостаточно памяти. Уменьшите количество фото, "
                "либо снизьте TRIPOSR_MC_RESOLUTION / VGGT_RESOLUTION / MAX_VIEWS в настройках ml-service.")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "Недостаточно места на диске"
    return f"Ошибка ML-пайплайна ({type(exc).__name__})"
