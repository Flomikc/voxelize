package com.example.photo3d.controller;

import com.example.photo3d.dto.GenerateOptions;
import com.example.photo3d.exception.ApiException;
import com.example.photo3d.model.UploadResponse;
import com.example.photo3d.service.PipelineService;
import com.example.photo3d.service.StorageService;
import com.example.photo3d.service.TaskService;
import java.nio.file.Path;
import java.util.List;
import java.util.UUID;
import org.springframework.core.task.TaskRejectedException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api")
public class UploadController {

    private final StorageService storage;
    private final TaskService tasks;
    private final PipelineService pipeline;

    public UploadController(StorageService storage, TaskService tasks, PipelineService pipeline) {
        this.storage = storage;
        this.tasks = tasks;
        this.pipeline = pipeline;
    }

    /**
     * Принимает 1..N изображений (поле «files») и необязательные параметры генерации:
     * detail_level (low|medium|high|ultra), polygon_limit (0 = без лимита | 1000..5000000), use_rembg (true|false).
     */
    @PostMapping(path = "/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<UploadResponse> upload(
            @RequestParam("files") List<MultipartFile> files,
            @RequestParam(name = "detail_level", defaultValue = "medium") String detailLevel,
            @RequestParam(name = "polygon_limit", defaultValue = "50000") String polygonLimit,
            @RequestParam(name = "use_rembg", defaultValue = "true") String useRembg) {

        GenerateOptions options = parseOptions(detailLevel, polygonLimit, useRembg);   // до записи файлов на диск
        String taskId = UUID.randomUUID().toString();

        List<Path> saved = storage.saveUploads(taskId, files);
        tasks.create(taskId, saved.size());

        try {
            pipeline.process(taskId, saved, options);            // @Async: вернётся мгновенно
        } catch (TaskRejectedException e) {                      // очередь переполнена
            tasks.remove(taskId);
            storage.deleteTask(taskId);
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE,
                    "Сервер занят: очередь переполнена, попробуйте позже");
        }
        return ResponseEntity.accepted().body(new UploadResponse(taskId));
    }

    private static GenerateOptions parseOptions(String detailLevel, String polygonLimit, String useRembg) {
        String level = detailLevel.trim().toLowerCase();
        if (!GenerateOptions.DETAIL_LEVELS.contains(level)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "detail_level: low, medium, high или ultra");
        }
        int limit;
        try {
            limit = Integer.parseInt(polygonLimit.trim());
        } catch (NumberFormatException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "polygon_limit должен быть числом");
        }
        if (limit != 0 && (limit < 1_000 || limit > 5_000_000)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "polygon_limit: 0 (без лимита) или от 1000 до 5 000 000");
        }
        return new GenerateOptions(level, limit, Boolean.parseBoolean(useRembg.trim()));
    }
}
