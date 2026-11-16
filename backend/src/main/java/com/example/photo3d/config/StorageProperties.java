package com.example.photo3d.config;

import java.nio.file.Path;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

/** Настройки файлового хранилища (app.storage.*). */
@ConfigurationProperties(prefix = "app.storage")
public record StorageProperties(
        @DefaultValue("./data") Path dataDir,
        @DefaultValue("20") int maxFiles,
        @DefaultValue("20") int maxFileSizeMb,
        @DefaultValue("24") int retentionHours) {
}
