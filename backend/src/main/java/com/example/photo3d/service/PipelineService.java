package com.example.photo3d.service;

import com.example.photo3d.config.MlProperties;
import com.example.photo3d.exception.MlServiceException;
import com.example.photo3d.model.MlGenerateRequest;
import com.example.photo3d.model.MlStatusResponse;
import com.example.photo3d.model.TaskStatus;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestClientException;

/**
 * Фоновый воркер: отправляет задачу в Python и опрашивает его статус,
 * транслируя прогресс в TaskService. Выполняется в пуле pipelineExecutor (1 поток),
 * поэтому задачи обрабатываются строго по очереди.
 */
@Service
public class PipelineService {

    private static final Logger log = LoggerFactory.getLogger(PipelineService.class);

    private final TaskService tasks;
    private final StorageService storage;
    private final MlClientService ml;
    private final MlProperties props;

    public PipelineService(TaskService tasks, StorageService storage, MlClientService ml, MlProperties props) {
        this.tasks = tasks;
        this.storage = storage;
        this.ml = ml;
        this.props = props;
    }

    @Async("pipelineExecutor")
    public void process(String taskId, List<Path> inputFiles) {
        log.info("[{}] Старт обработки, файлов: {}", taskId, inputFiles.size());
        try {
            tasks.update(taskId, TaskStatus.UPLOADING, TaskStatus.UPLOADING.defaultProgress(),
                    "Передача задачи в ML-сервис...");

            ml.submit(new MlGenerateRequest(
                    taskId,
                    inputFiles.stream().map(Path::toString).toList(),
                    storage.workDir(taskId).toString(),
                    storage.modelPath(taskId).toString()));

            pollUntilFinished(taskId);

        } catch (MlServiceException e) {
            log.error("[{}] Ошибка ML-сервиса: {}", taskId, e.getMessage(), e);
            tasks.fail(taskId, e.getMessage());
        } catch (Exception e) {
            log.error("[{}] Непредвиденная ошибка", taskId, e);
            tasks.fail(taskId, "Внутренняя ошибка обработки");
        }
    }

    /** Цикл опроса Python раз в poll-interval-ms до DONE / FAILED / таймаута. */
    private void pollUntilFinished(String taskId) {
        Instant deadline = Instant.now().plus(Duration.ofMinutes(props.timeoutMinutes()));
        int consecutiveFailures = 0;

        while (true) {
            if (Instant.now().isAfter(deadline)) {
                throw new MlServiceException("Превышено время обработки (" + props.timeoutMinutes() + " мин)");
            }
            sleep(props.pollIntervalMs());

            MlStatusResponse st;
            try {
                st = ml.fetchStatus(taskId);
                consecutiveFailures = 0;
            } catch (HttpClientErrorException.NotFound e) {
                // Python не знает такую задачу (например, был перезапущен) — ждать бессмысленно
                throw new MlServiceException("ML-сервис потерял задачу (возможно, был перезапущен)", e);
            } catch (RestClientException e) {
                // единичные сбои сети допустимы — терпим до max-poll-failures подряд
                if (++consecutiveFailures >= props.maxPollFailures()) {
                    throw new MlServiceException("ML-сервис перестал отвечать", e);
                }
                log.warn("[{}] Сбой опроса ({}/{}): {}", taskId, consecutiveFailures,
                        props.maxPollFailures(), e.getMessage());
                continue;
            }

            TaskStatus mapped = TaskStatus.parse(st.status()).orElse(null);
            if (mapped == null) {
                log.warn("[{}] Неизвестный статус от ML: {}", taskId, st.status());
                continue;
            }

            switch (mapped) {
                case DONE -> {
                    Path model = storage.modelPath(taskId);
                    if (!Files.isRegularFile(model)) {
                        throw new MlServiceException("ML-сервис сообщил об успехе, но model.glb не найден");
                    }
                    tasks.done(taskId);
                    log.info("[{}] Готово: {}", taskId, model);
                    return;
                }
                case FAILED -> {
                    String err = st.error() != null && !st.error().isBlank()
                            ? st.error() : "Ошибка генерации модели";
                    tasks.fail(taskId, err);
                    log.error("[{}] ML вернул ошибку: {}", taskId, err);
                    return;
                }
                default -> {
                    // ML-статус QUEUED (внутренний семафор Python) показываем как UPLOADING
                    TaskStatus shown = mapped == TaskStatus.QUEUED ? TaskStatus.UPLOADING : mapped;
                    int progress = st.progress() != null ? st.progress() : shown.defaultProgress();
                    tasks.update(taskId, shown, progress, st.message());
                }
            }
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();   // остановка приложения
            throw new MlServiceException("Обработка прервана остановкой сервера");
        }
    }
}
