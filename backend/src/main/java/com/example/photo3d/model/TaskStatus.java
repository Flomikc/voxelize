package com.example.photo3d.model;

import java.util.Optional;

/** Жизненный цикл задачи. Имена совпадают со статусами, которые присылает ml-service. */
public enum TaskStatus {
    QUEUED(0, "В очереди..."),
    UPLOADING(5, "Загрузка..."),
    DEPTH_ESTIMATION(10, "Оценка глубины..."),
    MESH_GENERATION(40, "Генерация меша..."),
    TEXTURING(75, "Наложение текстур..."),
    DONE(100, "Готово"),
    FAILED(100, "Ошибка");

    private final int defaultProgress;
    private final String label;

    TaskStatus(int defaultProgress, String label) {
        this.defaultProgress = defaultProgress;
        this.label = label;
    }

    public int defaultProgress() { return defaultProgress; }
    public String label() { return label; }
    public boolean isTerminal() { return this == DONE || this == FAILED; }

    /** Безопасный разбор статуса от ML-сервиса (неизвестные значения игнорируем). */
    public static Optional<TaskStatus> parse(String value) {
        if (value == null) return Optional.empty();
        try {
            return Optional.of(valueOf(value.trim().toUpperCase()));
        } catch (IllegalArgumentException e) {
            return Optional.empty();
        }
    }
}
