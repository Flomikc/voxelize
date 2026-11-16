package com.example.photo3d.model;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * Тело POST http://ml-service:8000/generate.
 * Пути абсолютные и одинаковы в обоих контейнерах (общий том, монтируется в /data).
 */
public record MlGenerateRequest(
        @JsonProperty("task_id") String taskId,
        @JsonProperty("input_files") List<String> inputFiles,
        @JsonProperty("work_dir") String workDir,
        @JsonProperty("output_path") String outputPath) {
}
