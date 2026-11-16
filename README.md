# Voxelize 🧊
**Voxelize** — веб-приложение для быстрой 3D-реконструкции объектов по фотографиям. 

Проект использует Java (Spring Boot) для организации веб-сервера и Python (FastAPI) для работы ML-моделей компьютерного зрения (TripoSR, VGGT). Готовая 3D-модель в формате `.glb` отображается прямо в браузере с помощью `<model-viewer>`.

## 🚀 Возможности
- Загрузка одного или нескольких фото (Multi-view).
- Автоматическое удаление фона (rembg).
- Генерация 3D-модели (TripoSR).
- Сохранение цвета (Vertex Colors).
- Интерактивный 3D-вьювер в браузере.

## 🛠 Требования
- Java 17+
- Python 3.10 (настоятельно рекомендуется использовать Miniconda)
- Git
- Maven

---

## 💻 Инструкция по локальному запуску

### Шаг 1: Клонирование репозитория и ML-библиотек
```bash
git clone https://github.com/Ваш_Ник/voxelize.git
cd voxelize/photo-to-3d

# Клонируем TripoSR вручную (так как мы используем кастомный патч)
cd ml-service
git clone https://github.com/VAST-AI-Research/TripoSR.git
```

### Шаг 2: Настройка ML-сервиса (Python)
Мы используем Miniconda, чтобы не зависеть от системного Python. Создаем окружение с Python 3.10:
```bash
# Установка Miniconda (если нет)
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda3
source $HOME/miniconda3/bin/activate

# Создание и активация окружения
conda create -n voxelize python=3.10 -y
conda activate voxelize

# Установка PyTorch (CPU-версия)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Установка инструментов сборки и библиотек
pip install scikit-build-core pybind11 cmake ninja scikit-image
pip install -r requirements.txt
pip install -r TripoSR/requirements.txt
```

**⚠️ Важный фикс (Компилятор C++20):**
Если при установке `torchmcubes` возникает ошибка C++20, мы заменяем его на `scikit-image`. 
Откройте файл `TripoSR/tsr/models/isosurface.py` и полностью замените его содержимое на:
```python
from typing import Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
from skimage.measure import marching_cubes as _sk_marching_cubes


class IsosurfaceHelper(nn.Module):
    points_range: Tuple[float, float] = (0, 1)

    @property
    def grid_vertices(self) -> torch.FloatTensor:
        raise NotImplementedError


class MarchingCubeHelper(IsosurfaceHelper):
    def __init__(self, resolution: int) -> None:
        super().__init__()
        self.resolution = resolution
        self._grid_vertices: Optional[torch.FloatTensor] = None

    @property
    def grid_vertices(self) -> torch.FloatTensor:
        if self._grid_vertices is None:
            x, y, z = (
                torch.linspace(*self.points_range, self.resolution),
                torch.linspace(*self.points_range, self.resolution),
                torch.linspace(*self.points_range, self.resolution),
            )
            x, y, z = torch.meshgrid(x, y, z, indexing="ij")
            verts = torch.cat(
                [x.reshape(-1, 1), y.reshape(-1, 1), z.reshape(-1, 1)], dim=-1
            ).reshape(-1, 3)
            self._grid_vertices = verts
        return self._grid_vertices

    def forward(
        self, level: torch.FloatTensor
    ) -> Tuple[torch.FloatTensor, torch.LongTensor]:
        vol = (
            (-level.view(self.resolution, self.resolution, self.resolution))
            .detach()
            .cpu()
            .numpy()
            .astype(np.float32)
        )
        try:
            verts, faces, _normals, _values = _sk_marching_cubes(vol, 0.0)
        except (ValueError, RuntimeError):
            return (
                torch.zeros((0, 3), dtype=torch.float32, device=level.device),
                torch.zeros((0, 3), dtype=torch.long, device=level.device),
            )

        verts = torch.from_numpy(verts).float().to(level.device)
        faces = torch.from_numpy(faces).long().to(level.device)

        verts = verts[:, [2, 1, 0]]
        faces = faces[:, [2, 1, 0]]

        return verts, faces
```

**Фикс путей к TripoSR:**
Откройте `ml-service/app/pipeline/single_view.py` и `multi_view.py`. В самом верху (после `from __future__ import annotations`) добавьте:
```python
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'TripoSR')))
```

### Шаг 3: Запуск

**Терминал 1 (ML-сервис):**
```bash
cd ml-service
conda activate voxelize
# Задаем АБСОЛЮТНЫЙ путь к папке data
export DATA_DIR="$(cd .. && pwd)/data"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Терминал 2 (Java-бэкенд):**
```bash
cd backend
# Задаем тот же АБСОЛЮТНЫЙ путь
export DATA_DIR="$(cd .. && pwd)/data"
mvn spring-boot:run
```

Откройте браузер и перейдите по адресу: **http://localhost:8081** (если порт 8080 занят).

---

## 🛠 Решение частых проблем

1. **Ошибка `Address already in use` (порт занят)**
   - В Java: измените `server.port` в `application.yml` на `8081`.
   - В Python: убейте зависший процесс командой `kill $(lsof -t -i:8000)`.

2. **Ошибка `HTTP 400` или `FileNotFoundError`**
   - Убедитесь, что вы задали `export DATA_DIR="$(cd .. && pwd)/data"` **в обоих** терминалах перед запуском.

3. **Ошибка `ModuleNotFoundError: No module named 'tsr'`**
   - Убедитесь, что вы сделали фикс путей (добавили `sys.path.append`) в `single_view.py` и `multi_view.py`.

4. **В Docker: `ml-service:8000` недоступен**
   - Если запускаете локально (без Docker), в `application.yml` Java-бэкенда должно быть `base-url: http://localhost:8000`, а не `http://ml-service:8000`.