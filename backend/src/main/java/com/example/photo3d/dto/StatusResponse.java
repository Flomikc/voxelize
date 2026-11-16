package com.example.photo3d.model;

/** Ответ фронтенду на GET /api/status/{id}. */
public record StatusResponse(
        String taskId,
        String status,
        String statusLabel,
        int progress,
        String message,
        String modelUrl,
        String error) {

    public static StatusResponse from(TaskInfo t) {
        String url = t.status() == TaskStatus.DONE ? "/api/model/" + t.id() + ".glb" : null;
        return new StatusResponse(t.id(), t.status().name(), t.status().label(),
                t.progress(), t.message(), url, t.error());
    }
}
