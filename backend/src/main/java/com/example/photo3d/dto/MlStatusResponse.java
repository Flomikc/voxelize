package com.example.photo3d.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/** Ответ GET http://ml-service:8000/status/{task_id}. */
@JsonIgnoreProperties(ignoreUnknown = true)
public record MlStatusResponse(
        String status,      // QUEUED | DEPTH_ESTIMATION | MESH_GENERATION | TEXTURING | DONE | FAILED
        Integer progress,   // 0..100, может отсутствовать
        String message,
        String error) {
}
