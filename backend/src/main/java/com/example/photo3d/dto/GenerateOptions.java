package com.example.photo3d.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.Set;

/** Параметры генерации (форма загрузки -> ML-сервис). Имена JSON совпадают с schemas.GenerateOptions в Python. */
public record GenerateOptions(
        @JsonProperty("detail_level") String detailLevel,
        @JsonProperty("polygon_limit") int polygonLimit,
        @JsonProperty("use_rembg") boolean useRembg) {

    public static final Set<String> DETAIL_LEVELS = Set.of("low", "medium", "high", "ultra");

    public static GenerateOptions defaults() {
        return new GenerateOptions("medium", 50_000, true);
    }
}
