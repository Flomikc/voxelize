package com.example.photo3d.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

/** Настройки взаимодействия с Python ML-сервисом (app.ml.*). */
@ConfigurationProperties(prefix = "app.ml")
public record MlProperties(
        @DefaultValue("http://ml-service:8000") String baseUrl,
        @DefaultValue("1000") long pollIntervalMs,
        @DefaultValue("60") long timeoutMinutes,
        @DefaultValue("10") int maxPollFailures,
        @DefaultValue("5") int submitRetries) {
}
