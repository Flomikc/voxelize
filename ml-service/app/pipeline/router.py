"""Выбор стратегии: 1 фото -> TripoSR, 2+ фото -> VGGT + Open3D."""
from __future__ import annotations

import logging

from app.pipeline import multi_view, single_view
from app.schemas import Job
from app.utils.progress import ProgressReporter

log = logging.getLogger(__name__)


def generate(job: Job, progress: ProgressReporter) -> None:
    n = len(job.input_files)
    if n == 1:
        log.info("[%s] 1 фото -> TripoSR", job.task_id)
        single_view.run(job, progress)
    else:
        log.info("[%s] %d фото -> VGGT + Poisson", job.task_id, n)
        multi_view.run(job, progress)
