package com.example.photo3d.service;

import com.example.photo3d.config.StorageProperties;
import com.example.photo3d.model.TaskInfo;
import com.example.photo3d.model.TaskStatus;
import jakarta.annotation.PostConstruct;
import java.io.IOException;
import java.nio.file.Files;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

/** Хранилище статусов задач в памяти + восстановление после рестарта и очистка старых задач. */
@Service
public class TaskService {

    private static final Logger log = LoggerFactory.getLogger(TaskService.class);

    private final ConcurrentHashMap<String, TaskInfo> tasks = new ConcurrentHashMap<>();
    private final StorageService storage;
    private final StorageProperties props;

    public TaskService(StorageService storage, StorageProperties props) {
        this.storage = storage;
        this.props = props;
    }

    /**
     * После перезапуска Java карта пуста. Восстанавливаем её по диску:
     * есть model.glb -> DONE, иначе задача была прервана -> FAILED.
     */
    @PostConstruct
    void restoreFromDisk() {
        for (String id : storage.listTaskIds()) {
            try {
                Instant ts = Files.getLastModifiedTime(storage.taskDir(id)).toInstant();
                TaskInfo info;
                if (Files.exists(storage.modelPath(id))) {
                    info = new TaskInfo(id, TaskStatus.DONE, 100, TaskStatus.DONE.label(), null, 0, ts, ts);
                } else {
                    info = new TaskInfo(id, TaskStatus.FAILED, 100, TaskStatus.FAILED.label(),
                            "Обработка была прервана перезапуском сервера", 0, ts, ts);
                }
                tasks.put(id, info);
            } catch (IOException e) {
                log.warn("Не удалось восстановить задачу {}", id, e);
            }
        }
        log.info("Восстановлено задач с диска: {}", tasks.size());
    }

    public TaskInfo create(String id, int filesCount) {
        TaskInfo info = TaskInfo.queued(id, filesCount);
        tasks.put(id, info);
        return info;
    }

    public Optional<TaskInfo> find(String id) {
        return Optional.ofNullable(tasks.get(id));
    }

    /** Обновляет прогресс. Терминальные статусы не перезаписываются, прогресс не откатывается назад. */
    public void update(String id, TaskStatus status, int progress, String message) {
        tasks.computeIfPresent(id, (k, t) -> {
            if (t.status().isTerminal()) return t;
            int p = Math.max(t.progress(), Math.min(progress, 99));
            return t.with(status, p, message != null && !message.isBlank() ? message : status.label(), null);
        });
    }

    public void done(String id) {
        tasks.computeIfPresent(id, (k, t) ->
                t.status() == TaskStatus.FAILED ? t
                        : t.with(TaskStatus.DONE, 100, TaskStatus.DONE.label(), null));
    }

    public void fail(String id, String error) {
        tasks.computeIfPresent(id, (k, t) ->
                t.status() == TaskStatus.DONE ? t
                        : t.with(TaskStatus.FAILED, 100, TaskStatus.FAILED.label(), error));
    }

    public void remove(String id) {
        tasks.remove(id);
    }

    /** Раз в 30 минут удаляем завершённые задачи старше retention-hours (экономим диск). */
    @Scheduled(initialDelayString = "PT30M", fixedDelayString = "PT30M")
    void cleanupOld() {
        Instant limit = Instant.now().minus(props.retentionHours(), ChronoUnit.HOURS);
        tasks.values().stream()
                .filter(t -> t.status().isTerminal() && t.updatedAt().isBefore(limit))
                .forEach(t -> {
                    log.info("Удаляем устаревшую задачу {}", t.id());
                    tasks.remove(t.id());
                    storage.deleteTask(t.id());
                });
    }
}
