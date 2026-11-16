package com.example.photo3d;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@ConfigurationPropertiesScan
@EnableAsync        // фоновая обработка задач (PipelineService)
@EnableScheduling   // периодическая очистка старых задач (TaskService)
public class Photo3dApplication {
    public static void main(String[] args) {
        SpringApplication.run(Photo3dApplication.class, args);
    }
}
