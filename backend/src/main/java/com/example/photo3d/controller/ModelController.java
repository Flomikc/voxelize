package com.example.photo3d.controller;

import com.example.photo3d.exception.ApiException;
import com.example.photo3d.service.StorageService;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
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
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class ModelController {

    private static final MediaType GLB = MediaType.parseMediaType("model/gltf-binary");

    private final StorageService storage;

    public ModelController(StorageService storage) {
        this.storage = storage;
    }

    /**
     * GET /api/model/{taskId}.glb            — для <model-viewer> (inline)
     * GET /api/model/{taskId}.glb?download=1 — скачивание (Content-Disposition: attachment)
     * Spring сам обрабатывает Range-запросы для Resource.
     */
    @GetMapping("/model/{filename:.+}")
    public ResponseEntity<Resource> model(@PathVariable String filename,
                                          @RequestParam(defaultValue = "false") boolean download) {
        if (!filename.endsWith(".glb")) {
            throw new ApiException(HttpStatus.NOT_FOUND, "Модель не найдена");
        }
        String taskId = filename.substring(0, filename.length() - 4);
        Path file = storage.modelPath(taskId);   // проверит UUID и path traversal
        if (!Files.isRegularFile(file)) {
            throw new ApiException(HttpStatus.NOT_FOUND, "Модель не найдена или ещё не готова");
        }

        ResponseEntity.BodyBuilder response = ResponseEntity.ok()
                .contentType(GLB)
                .cacheControl(CacheControl.maxAge(1, TimeUnit.HOURS).cachePrivate());
        if (download) {
            response.header(HttpHeaders.CONTENT_DISPOSITION,
                    ContentDisposition.attachment().filename("model-" + taskId.substring(0, 8) + ".glb").build().toString());
        }
        return response.body(new FileSystemResource(file));
    }
}
