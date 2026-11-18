package com.example.photo3d.dto;

import java.util.Map;

/** Ответ фронтенду: ссылка на вариант и его статистика. */
public record ProcessResponse(
        String url,
        String format,
        int vertices,
        int faces,
        int baseFaces,
        long sizeBytes,
        boolean textured,
        Map<String, Object> repair) {
}
