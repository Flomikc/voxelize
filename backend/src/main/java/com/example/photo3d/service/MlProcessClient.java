package com.example.photo3d.service;

import com.example.photo3d.config.MlProperties;
import com.example.photo3d.dto.MlProcessResponse;
import com.example.photo3d.dto.ProcessRequest;
import com.example.photo3d.exception.ApiException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.http.HttpClient;
import java.time.Duration;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;

/**
 * Клиент для POST /process. Отдельный от MlClientService: модификаторы (Decimate, Subdivision) на слабом ПК
 * могут считаться десятки секунд, поэтому таймаут чтения здесь заметно больше, чем у опроса статуса.
 */
@Service
public class MlProcessClient {

    private static final Logger log = LoggerFactory.getLogger(MlProcessClient.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final RestClient client;

    public MlProcessClient(RestClient.Builder builder, MlProperties props) {
        HttpClient http = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)      // см. RestClientConfig: h2c-upgrade ломает uvicorn
                .connectTimeout(Duration.ofSeconds(5))
                .build();
        JdkClientHttpRequestFactory factory = new JdkClientHttpRequestFactory(http);
        factory.setReadTimeout(Duration.ofSeconds(180));
        this.client = builder.baseUrl(props.baseUrl()).requestFactory(factory).build();
    }

    public MlProcessResponse process(String taskId, ProcessRequest request) {
        try {
            MlProcessResponse resp = client.post()
                    .uri("/process/{id}", taskId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(MlProcessResponse.class);
            if (resp == null || resp.fileName() == null) {
                throw new ApiException(HttpStatus.BAD_GATEWAY, "Пустой ответ ML-сервиса");
            }
            return resp;
        } catch (HttpStatusCodeException e) {
            HttpStatus st = HttpStatus.resolve(e.getStatusCode().value());
            boolean passThrough = st == HttpStatus.CONFLICT || st == HttpStatus.UNPROCESSABLE_ENTITY
                    || st == HttpStatus.NOT_FOUND;
            log.warn("ML /process вернул {}: {}", e.getStatusCode(), e.getResponseBodyAsString());
            throw new ApiException(passThrough ? st : HttpStatus.BAD_GATEWAY, detail(e.getResponseBodyAsString()));
        } catch (ResourceAccessException e) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "ML-сервис недоступен или не успел ответить");
        }
    }

    /** FastAPI кладёт текст ошибки в поле "detail" (для ошибок валидации это массив — тогда общий текст). */
    private static String detail(String body) {
        try {
            JsonNode d = MAPPER.readTree(body).path("detail");
            if (d.isTextual() && !d.asText().isBlank()) return d.asText();
        } catch (Exception ignored) { /* не JSON */ }
        return "Не удалось применить модификаторы";
    }
}
