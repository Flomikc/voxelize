package com.example.photo3d.service;

import com.example.photo3d.config.StorageProperties;
import com.example.photo3d.exception.ApiException;
import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

/**
 * Работа с диском. Структура:
 *   {dataDir}/tasks/{taskId}/input/img_001.jpg ...
 *   {dataDir}/tasks/{taskId}/work/       (пишет Python)
 *   {dataDir}/tasks/{taskId}/model.glb   (пишет Python)
 */
@Service
public class StorageService {

    private static final Logger log = LoggerFactory.getLogger(StorageService.class);
    private static final Pattern UUID_RE =
            Pattern.compile("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");

    private final StorageProperties props;
    private final Path tasksRoot;

    public StorageService(StorageProperties props) throws IOException {
        this.props = props;
        this.tasksRoot = props.dataDir().toAbsolutePath().normalize().resolve("tasks");
        Files.createDirectories(tasksRoot);
        log.info("Каталог задач: {}", tasksRoot);
    }

    public static boolean isValidTaskId(String id) {
        return id != null && UUID_RE.matcher(id).matches();
    }

    /** Путь к каталогу задачи с защитой от path traversal. */
    public Path taskDir(String taskId) {
        if (!isValidTaskId(taskId)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "Некорректный идентификатор задачи");
        }
        Path dir = tasksRoot.resolve(taskId).normalize();
        if (!dir.startsWith(tasksRoot)) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "Некорректный идентификатор задачи");
        }
        return dir;
    }

    public Path inputDir(String taskId) { return taskDir(taskId).resolve("input"); }
    public Path workDir(String taskId) { return taskDir(taskId).resolve("work"); }
    public Path modelPath(String taskId) { return taskDir(taskId).resolve("model.glb"); }

    /**
     * Валидирует и сохраняет загруженные изображения.
     * Имена файлов НЕ берутся от пользователя (генерируются), тип определяется по magic bytes.
     */
    public List<Path> saveUploads(String taskId, List<MultipartFile> files) {
        validateCount(files);
        Path input = inputDir(taskId);
        List<Path> saved = new ArrayList<>();
        try {
            Files.createDirectories(input);
            Files.createDirectories(workDir(taskId));

            long maxBytes = props.maxFileSizeMb() * 1024L * 1024L;
            int index = 1;
            for (MultipartFile file : files) {
                if (file.isEmpty()) {
                    throw new ApiException(HttpStatus.BAD_REQUEST, "Один из файлов пустой");
                }
                if (file.getSize() > maxBytes) {
                    throw new ApiException(HttpStatus.PAYLOAD_TOO_LARGE,
                            "Файл больше " + props.maxFileSizeMb() + " МБ");
                }
                try (InputStream raw = file.getInputStream();
                     BufferedInputStream in = new BufferedInputStream(raw)) {
                    String ext = detectExtension(in);
                    Path target = input.resolve(String.format("img_%03d.%s", index++, ext));
                    Files.copy(in, target, StandardCopyOption.REPLACE_EXISTING);
                    saved.add(target);
                }
            }
            return saved;
        } catch (IOException e) {
            deleteTask(taskId);
            throw new UncheckedIOException(e);
        } catch (RuntimeException e) {
            deleteTask(taskId);   // не оставляем мусор при ошибке валидации
            throw e;
        }
    }

    private void validateCount(List<MultipartFile> files) {
        if (files == null || files.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "Загрузите хотя бы одно изображение");
        }
        if (files.size() > props.maxFiles()) {
            throw new ApiException(HttpStatus.BAD_REQUEST,
                    "Слишком много файлов (максимум " + props.maxFiles() + ")");
        }
    }

    /** Определяет формат по сигнатуре: JPEG (FF D8 FF) или PNG (89 50 4E 47 0D 0A 1A 0A). */
    private String detectExtension(BufferedInputStream in) throws IOException {
        in.mark(16);
        byte[] head = in.readNBytes(8);
        in.reset();
        if (head.length >= 3 && (head[0] & 0xFF) == 0xFF && (head[1] & 0xFF) == 0xD8 && (head[2] & 0xFF) == 0xFF) {
            return "jpg";
        }
        if (head.length >= 8 && (head[0] & 0xFF) == 0x89 && head[1] == 'P' && head[2] == 'N' && head[3] == 'G'
                && head[4] == 0x0D && head[5] == 0x0A && head[6] == 0x1A && head[7] == 0x0A) {
            return "png";
        }
        throw new ApiException(HttpStatus.UNSUPPORTED_MEDIA_TYPE, "Поддерживаются только JPG и PNG");
    }

    /** Идентификаторы задач, существующих на диске (для восстановления после рестарта). */
    public List<String> listTaskIds() {
        try (Stream<Path> s = Files.list(tasksRoot)) {
            return s.filter(Files::isDirectory)
                    .map(p -> p.getFileName().toString())
                    .filter(StorageService::isValidTaskId)
                    .toList();
        } catch (IOException e) {
            log.warn("Не удалось прочитать каталог задач", e);
            return List.of();
        }
    }

    public void deleteTask(String taskId) {
        Path dir = taskDir(taskId);
        if (!Files.exists(dir)) return;
        try (Stream<Path> walk = Files.walk(dir)) {
            walk.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException e) {
                    log.warn("Не удалось удалить {}", p, e);
                }
            });
        } catch (IOException e) {
            log.warn("Не удалось очистить каталог задачи {}", taskId, e);
        }
    }
}
