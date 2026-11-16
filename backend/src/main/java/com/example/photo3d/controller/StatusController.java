package com.example.photo3d.controller;

import com.example.photo3d.exception.ApiException;
import com.example.photo3d.model.StatusResponse;
import com.example.photo3d.service.StorageService;
import com.example.photo3d.service.TaskService;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class StatusController {

    private final TaskService tasks;

    public StatusController(TaskService tasks) {
        this.tasks = tasks;
    }

    @GetMapping("/status/{taskId}")
    public ResponseEntity<StatusResponse> status(@PathVariable String taskId) {
        if (!StorageService.isValidTaskId(taskId)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "Некорректный идентификатор задачи");
        }
        return tasks.find(taskId)
                .map(t -> ResponseEntity.ok()
                        .cacheControl(CacheControl.noStore())   // статус не должен кэшироваться
                        .body(StatusResponse.from(t)))
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "Задача не найдена"));
    }
}
