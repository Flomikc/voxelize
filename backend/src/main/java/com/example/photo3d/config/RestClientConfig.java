package com.example.photo3d.config;

import java.net.http.HttpClient;
import java.time.Duration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

@Configuration
public class RestClientConfig {

    @Bean
    public RestClient mlRestClient(RestClient.Builder builder, MlProperties props) {
        HttpClient httpClient = HttpClient.newBuilder()
                // Принудительно HTTP/1.1: JDK HttpClient по умолчанию пытается h2c-upgrade,
                // что с uvicorn приводит к обрывам соединения на POST.
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(5))
                .build();

        JdkClientHttpRequestFactory factory = new JdkClientHttpRequestFactory(httpClient);
        // /generate и /status отвечают быстро (тяжёлая работа идёт в фоне на стороне Python)
        factory.setReadTimeout(Duration.ofSeconds(15));

        return builder.baseUrl(props.baseUrl()).requestFactory(factory).build();
    }
}
