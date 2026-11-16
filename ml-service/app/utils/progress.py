"""Статусы задач: в памяти + status.json на диске (переживает рестарт сервиса)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from app.schemas import TERMINAL, UUID_PATTERN, Stage

log = logging.getLogger(__name__)
_UUID_RE = re.compile(UUID_PATTERN)


def is_valid_task_id(task_id: str) -> bool:
    return bool(task_id) and _UUID_RE.match(task_id) is not None


class TaskRegistry:
    def __init__(self, tasks_root: Path):
        self._root = tasks_root
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}

    # ---------- запись ----------
    def register(self, task_id: str) -> bool:
        """True — задача новая; False — уже известна (повторный POST идемпотентен)."""
        with self._lock:
            if task_id in self._tasks:
                return False
            snap = self._snapshot(Stage.QUEUED.value, 0, "В очереди ML-сервиса", None)
            self._tasks[task_id] = snap
            self._persist(task_id, snap)
            return True

    def update(self, task_id: str, status: str, progress: int, message: str,
               error: Optional[str] = None) -> None:
        with self._lock:
            current = self._tasks.get(task_id)
            if current is None or current["status"] in TERMINAL:
                return                                   # терминальные статусы не перезаписываем
            if status not in TERMINAL:
                progress = max(progress, current["progress"])   # прогресс не откатывается
            snap = self._snapshot(status, progress, message, error)
            self._tasks[task_id] = snap
            self._persist(task_id, snap)

    # ---------- чтение ----------
    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            snap = self._tasks.get(task_id)
        if snap is not None:
            return dict(snap)
        return self._load_from_disk(task_id)

    # ---------- внутреннее ----------
    @staticmethod
    def _snapshot(status: str, progress: int, message: str, error: Optional[str]) -> dict:
        return {"status": status, "progress": int(progress), "message": message,
                "error": error, "updated_at": time.time()}

    def _status_path(self, task_id: str) -> Path:
        return self._root / task_id / "status.json"

    def _persist(self, task_id: str, snap: dict) -> None:
        path = self._status_path(task_id)
        if not path.parent.is_dir():
            return
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)                        # атомарно: читатель не увидит половину файла
        except OSError:
            log.warning("Не удалось записать %s", path, exc_info=True)

    def _load_from_disk(self, task_id: str) -> Optional[dict]:
        if not is_valid_task_id(task_id):
            return None
        path = self._status_path(task_id)
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # Задача есть на диске, но в памяти её нет => сервис перезапускали посреди работы.
        if snap.get("status") not in TERMINAL:
            return self._snapshot(Stage.FAILED.value, 100, "Ошибка",
                                  "ML-сервис был перезапущен во время обработки")
        return snap


class ProgressReporter:
    """Передаётся в пайплайн: progress(Stage.MESH_GENERATION, 55, "Poisson...")."""

    def __init__(self, registry: TaskRegistry, task_id: str):
        self._registry = registry
        self._task_id = task_id

    def __call__(self, stage: Stage, progress: int, message: str) -> None:
        log.info("[%s] %s %d%% — %s", self._task_id, stage.value, progress, message)
        self._registry.update(self._task_id, stage.value, min(progress, 99), message)

    def done(self) -> None:
        self._registry.update(self._task_id, Stage.DONE.value, 100, "Готово")

    def fail(self, error: str) -> None:
        self._registry.update(self._task_id, Stage.FAILED.value, 100, "Ошибка", error)
