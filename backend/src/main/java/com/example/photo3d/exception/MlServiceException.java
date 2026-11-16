package com.example.photo3d.exception;

/** Сбой взаимодействия с ML-сервисом (недоступен, отклонил задачу, таймаут). */
public class MlServiceException extends RuntimeException {
    public MlServiceException(String message) { super(message); }
    public MlServiceException(String message, Throwable cause) { super(message, cause); }
}
