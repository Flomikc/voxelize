package com.example.photo3d.controller;

import com.example.photo3d.dto.MlProcessResponse;
import com.example.photo3d.dto.ProcessRequest;
import com.example.photo3d.dto.ProcessResponse;
import com.example.photo3d.exception.ApiException;
import com.example.photo3d.model.TaskInfo;
import com.example.photo3d.model.TaskStatus;
import com.example.photo3d.service.MlProcessClient;
import com.example.photo3d.service.StorageService;
import com.example.photo3d.service.TaskService;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.CacheControl;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Модификаторы готовой модели.
 *   POST /api/process/{taskId}            — применить Decimate / Subdivision / Smoothing, вернуть ссылку на вариант
 *   GET  /api/variant/{taskId}/{name}     — отдать файл варианта (glb / stl / ply) из post/variants/
 * Имя файла проверяется по белому списку символов — выйти за пределы каталога задачи нельзя.
 */
@RestController
@RequestMapping("/api")
public class ProcessController {

    private static final Pattern VARIANT_NAME = Pattern.compile("^[A-Za-z0-9_]+\\.(glb|stl|ply)$");
    private static final Set<String> FORMATS = Set.of("glb", "stl", "ply");

    private final TaskService tasks;
    private final StorageService storage;
    private final MlProcessClient ml;

    public ProcessController(TaskService tasks, StorageService storage, MlProcessClient ml) {
        this.tasks = tasks;
        this.storage = storage;
        this.ml = ml;
    }

    @PostMapping("/process/{taskId}")
    public ResponseEntity<ProcessResponse> process(@PathVariable String taskId,
                                                   @RequestBody(required = false) ProcessRequest body) {
        if (!StorageService.isValidTaskId(taskId)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "Некорректный идентификатор задачи");
        }
        TaskInfo task = tasks.find(taskId).orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "Задача не найдена"));
        if (task.status() != TaskStatus.DONE) {
            throw new ApiException(HttpStatus.CONFLICT, "Модель ещё не готова");
        }

        ProcessRequest req = normalize(body);
        MlProcessResponse r = ml.process(taskId, req);
        if (!VARIANT_NAME.matcher(r.fileName()).matches()) {
            throw new ApiException(HttpStatus.BAD_GATEWAY, "ML-сервис вернул некорректное имя файла");
        }
        Map<String, Object> repair = r.repair() == null ? Map.of() : r.repair();
        return ResponseEntity.ok(new ProcessResponse("/api/variant/" + taskId + "/" + r.fileName(), r.format(),
                r.vertices(), r.faces(), r.baseFaces(), r.sizeBytes(), r.textured(), repair));
    }

    @GetMapping("/variant/{taskId}/{name:.+}")
    public ResponseEntity<Resource> variant(@PathVariable String taskId, @PathVariable String name,
                                            @RequestParam(defaultValue = "false") boolean download) {
        if (!VARIANT_NAME.matcher(name).matches()) {
            throw new ApiException(HttpStatus.NOT_FOUND, "Файл не найден");
        }
        Path file = storage.taskDir(taskId).resolve("post").resolve("variants").resolve(name);   // taskDir проверит UUID
        if (!Files.isRegularFile(file)) {
            throw new ApiException(HttpStatus.NOT_FOUND, "Файл не найден или ещё не создан");
        }
        String ext = name.substring(name.lastIndexOf('.') + 1);
        MediaType type = switch (ext) {
            case "glb" -> MediaType.parseMediaType("model/gltf-binary");
            case "stl" -> MediaType.parseMediaType("model/stl");
            default -> MediaType.APPLICATION_OCTET_STREAM;
        };
        ResponseEntity.BodyBuilder resp = ResponseEntity.ok()
                .contentType(type)
                // имя файла содержит параметры варианта, поэтому содержимое по нему не меняется
                .cacheControl(CacheControl.maxAge(1, TimeUnit.HOURS).cachePrivate());
        if (download) {
            resp.header(HttpHeaders.CONTENT_DISPOSITION,
                    ContentDisposition.attachment().filename("voxelize-" + taskId.substring(0, 8) + "-" + name).build().toString());
        }
        return resp.body(new FileSystemResource(file));
    }

    /** Значения по умолчанию + проверка диапазонов (зеркалит ограничения Python-сервиса). */
    private static ProcessRequest normalize(ProcessRequest r) {
        int smoothing = r == null || r.smoothing() == null ? 0 : r.smoothing();
        double ratio = r == null || r.decimateRatio() == null ? 1.0 : r.decimateRatio();
        int sub = r == null || r.subdivision() == null ? 0 : r.subdivision();
        String format = r == null || r.format() == null ? "glb" : r.format().toLowerCase();
        double size = r == null || r.sizeMm() == null ? 100.0 : r.sizeMm();

        if (smoothing < 0 || smoothing > 10) throw bad("Сглаживание: от 0 до 10");
        if (!(ratio >= 0.02 && ratio <= 1.0)) throw bad("Decimate: от 2% до 100%");
        if (sub < 0 || sub > 2) throw bad("Subdivision: 0, 1 или 2 уровня");
        if (!FORMATS.contains(format)) throw bad("Формат: glb, stl или ply");
        if (!(size > 0 && size <= 2000)) throw bad("Размер: от 1 до 2000 мм");
        return new ProcessRequest(smoothing, ratio, sub, format, size);
    }

    private static ApiException bad(String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, message);
    }
}
