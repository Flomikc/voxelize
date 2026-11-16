package com.example.photo3d.model;

import java.time.Instant;

/** Неизменяемый снимок состояния задачи (обновляется атомарно через ConcurrentHashMap.compute). */
public record TaskInfo(
        String id,
        TaskStatus status,
        int progress,
        String message,
        String error,
        int filesCount,
        Instant createdAt,
        Instant updatedAt) {

    public static TaskInfo queued(String id, int filesCount) {
        Instant now = Instant.now();
        return new TaskInfo(id, TaskStatus.QUEUED, 0, TaskStatus.QUEUED.label(), null, filesCount, now, now);
    }

    public TaskInfo with(TaskStatus status, int progress, String message, String error) {
        return new TaskInfo(id, status, progress, message, error, filesCount, createdAt, Instant.now());
    }
}
