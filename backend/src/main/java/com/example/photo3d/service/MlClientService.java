package com.example.photo3d.service;

import com.example.photo3d.config.MlProperties;
import com.example.photo3d.exception.MlServiceException;
import com.example.photo3d.model.MlGenerateRequest;
import com.example.photo3d.model.MlStatusResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

/** Тонкий HTTP-клиент к FastAPI. Контракт: POST /generate (202) и GET /status/{task_id}. */
@Service
public class MlClientService {

    private static final Logger log = LoggerFactory.getLogger(MlClientService.class);

    private final RestClient client;
    private final MlProperties props;

    public MlClientService(RestClient mlRestClient, MlProperties props) {
        this.client = mlRestClient;
        this.props = props;
    }

    /** Ставит задачу в работу. Повторяет попытку, если ML-сервис ещё не поднялся. */
    public void submit(MlGenerateRequest request) {
        for (int attempt = 1; ; attempt++) {
            try {
                client.post()
                        .uri("/generate")
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(request)
                        .retrieve()
                        .toBodilessEntity();
                return;
            } catch (ResourceAccessException e) {            // сеть / таймаут / сервис не запущен
                if (attempt >= props.submitRetries()) {
                    throw new MlServiceException("ML-сервис недоступен", e);
                }
                log.warn("ML-сервис недоступен (попытка {}/{}), повтор через 2 с", attempt, props.submitRetries());
                sleepQuietly(2000);
            } catch (RestClientResponseException e) {        // 4xx/5xx
                throw new MlServiceException("ML-сервис отклонил задачу (HTTP "
                        + e.getStatusCode().value() + ")", e);
            }
        }
    }

    /** Запрашивает статус. Исключения пробрасываются вызывающему (PipelineService решает, что делать). */
    public MlStatusResponse fetchStatus(String taskId) {
        MlStatusResponse resp = client.get()
                .uri("/status/{id}", taskId)
                .retrieve()
                .body(MlStatusResponse.class);
        if (resp == null) {
            throw new MlServiceException("Пустой ответ ML-сервиса");
        }
        return resp;
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new MlServiceException("Обработка прервана");
        }
    }
}
