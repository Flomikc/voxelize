package com.example.photo3d.controller;

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

    /** Принимает 1..N изображений (поле формы «files»), возвращает task_id. */
    @PostMapping(path = "/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<UploadResponse> upload(@RequestParam("files") List<MultipartFile> files) {
        String taskId = UUID.randomUUID().toString();

        List<Path> saved = storage.saveUploads(taskId, files);   // валидация + запись на диск
        tasks.create(taskId, saved.size());

        try {
            pipeline.process(taskId, saved);                     // @Async: вернётся мгновенно
        } catch (TaskRejectedException e) {                      // очередь переполнена
            tasks.remove(taskId);
            storage.deleteTask(taskId);
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE,
                    "Сервер занят: очередь переполнена, попробуйте позже");
        }
        return ResponseEntity.accepted().body(new UploadResponse(taskId));
    }
}
