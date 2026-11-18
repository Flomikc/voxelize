package com.example.photo3d.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Запрос модификаторов: фронтенд -> Java -> Python (одно и то же тело).
 * Поля необязательные; ProcessController подставляет значения по умолчанию и проверяет диапазоны.
 */
public record ProcessRequest(
        @JsonProperty("smoothing") Integer smoothing,            // 0..10, Taubin
        @JsonProperty("decimate_ratio") Double decimateRatio,    // 0.02..1.0, доля оставляемых полигонов
        @JsonProperty("subdivision") Integer subdivision,        // 0..2
        @JsonProperty("format") String format,                   // glb | stl | ply
        @JsonProperty("size_mm") Double sizeMm) {                // для stl/ply
}
