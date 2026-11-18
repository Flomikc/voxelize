package com.example.photo3d.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.Map;

/** Ответ POST http://ml-service:8000/process/{task_id}. */
@JsonIgnoreProperties(ignoreUnknown = true)
public record MlProcessResponse(
        @JsonProperty("file_name") String fileName,
        String format,
        int vertices,
        int faces,
        @JsonProperty("base_faces") int baseFaces,
        @JsonProperty("size_bytes") long sizeBytes,
        boolean textured,
        Map<String, Object> repair) {
}
