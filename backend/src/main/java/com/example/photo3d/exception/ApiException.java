package com.example.photo3d.exception;

import org.springframework.http.HttpStatus;

/** Ошибка с заранее известным HTTP-статусом и сообщением, безопасным для показа пользователю. */
public class ApiException extends RuntimeException {
    private final HttpStatus status;

    public ApiException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public HttpStatus status() { return status; }
}
