package com.example.photo3d.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

@Configuration
public class AsyncConfig {

    /**
     * Очередь с ОДНИМ воркером: на слабой машине две параллельные ML-задачи
     * исчерпают память. Остальные задачи ждут в очереди (статус QUEUED).
     * При переполнении очереди ThreadPoolTaskExecutor бросает TaskRejectedException
     * (политика по умолчанию — AbortPolicy), контроллер превращает её в HTTP 503.
     */
    @Bean(name = "pipelineExecutor")
    public ThreadPoolTaskExecutor pipelineExecutor(@Value("${app.queue-capacity:20}") int queueCapacity) {
        ThreadPoolTaskExecutor ex = new ThreadPoolTaskExecutor();
        ex.setCorePoolSize(1);
        ex.setMaxPoolSize(1);
        ex.setQueueCapacity(queueCapacity);
        ex.setThreadNamePrefix("pipeline-");
        ex.setWaitForTasksToCompleteOnShutdown(true);
        ex.setAwaitTerminationSeconds(20);
        return ex;
    }
}
