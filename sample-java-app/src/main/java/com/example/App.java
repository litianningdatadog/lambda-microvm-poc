package com.example;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import jdk.jshell.JShell;
import jdk.jshell.Snippet;
import jdk.jshell.SnippetEvent;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * Sample guest application that implements Lambda MicroVMs lifecycle hooks.
 *
 * Listens on port 8080. /execute interprets Java source via {@link JShell}
 * (a fresh interpreter per request, matching the Python sample's stateless
 * snippet-runner semantics).
 */
public final class App {

    private static final String BASE_PATH = "/aws/lambda-microvms/runtime/beta/v1";
    private static final int PORT = 8080;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static volatile String microVmId = null;

    private App() {}

    public static void main(String[] args) throws IOException {
        log("Starting sample guest application on port " + PORT);

        HttpServer server = HttpServer.create(new InetSocketAddress("0.0.0.0", PORT), 0);

        server.createContext("/health", App::handleHealth);
        server.createContext(BASE_PATH + "/ready", emptyHook("Ready"));
        server.createContext(BASE_PATH + "/launch", App::handleLaunch);
        server.createContext(BASE_PATH + "/resume", emptyHook("Resume"));
        server.createContext(BASE_PATH + "/suspend", emptyHook("Suspend"));
        server.createContext(BASE_PATH + "/terminate", emptyHook("Terminate"));
        server.createContext("/execute", App::handleExecute);

        server.setExecutor(null);
        server.start();
        printSampleCommands();
    }

    private static void handleHealth(HttpExchange exchange) throws IOException {
        log("Health check called [ts=" + nowTs() + ", microVmId=" + microVmId + "]");
        writeJson(exchange, 200, Map.of("status", "healthy"));
    }

    private static HttpHandler emptyHook(String name) {
        return exchange -> {
            log(name + " hook called [ts=" + nowTs() + ", microVmId=" + microVmId + "]");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        };
    }

    private static void handleLaunch(HttpExchange exchange) throws IOException {
        byte[] raw = exchange.getRequestBody().readAllBytes();
        JsonNode data = raw.length == 0 ? MAPPER.createObjectNode() : MAPPER.readTree(raw);
        microVmId = data.path("microVmId").asText(null);
        String meshIpv6 = data.path("meshIpv6Address").asText(null);
        log("Launch hook called — ts=" + nowTs() + ", microVmId=" + microVmId
                + ", meshIpv6Address=" + meshIpv6);
        exchange.sendResponseHeaders(200, -1);
        exchange.close();
    }

    private static void handleExecute(HttpExchange exchange) throws IOException {
        try {
            byte[] raw = exchange.getRequestBody().readAllBytes();
            JsonNode data = raw.length == 0 ? MAPPER.createObjectNode() : MAPPER.readTree(raw);
            String code = data.path("code").asText("");
            if (code.isEmpty()) {
                writeJson(exchange, 400, Map.of("error", "No code provided"));
                return;
            }

            log("Execute called [ts=" + nowTs() + ", microVmId=" + microVmId + "]");

            ByteArrayOutputStream out = new ByteArrayOutputStream();
            ByteArrayOutputStream err = new ByteArrayOutputStream();
            try (JShell shell = JShell.builder()
                    .out(new PrintStream(out, true, StandardCharsets.UTF_8))
                    .err(new PrintStream(err, true, StandardCharsets.UTF_8))
                    .build()) {

                StringBuilder errors = new StringBuilder();
                Function<String, List<SnippetEvent>> snippetRunner = shell::eval;
                List<SnippetEvent> events = snippetRunner.apply(code);
                for (SnippetEvent ev : events) {
                    if (ev.status() == Snippet.Status.REJECTED) {
                        errors.append("Rejected: ").append(ev.snippet().source()).append('\n');
                    }
                    if (ev.exception() != null) {
                        errors.append(ev.exception().getMessage()).append('\n');
                    }
                }

                String stdout = out.toString(StandardCharsets.UTF_8);
                String stderr = err.toString(StandardCharsets.UTF_8) + errors;

                if (errors.length() > 0) {
                    writeJson(exchange, 200, Map.of(
                            "success", false,
                            "error", errors.toString(),
                            "stderr", stderr));
                } else {
                    writeJson(exchange, 200, Map.of(
                            "success", true,
                            "output", stdout,
                            "stderr", stderr));
                }
            }
        } catch (Exception e) {
            writeJson(exchange, 500, Map.of("error", e.toString()));
        }
    }

    private static void writeJson(HttpExchange exchange, int status, Object obj) throws IOException {
        byte[] body = MAPPER.writeValueAsBytes(obj);
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, body.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(body);
        }
    }

    private static String nowTs() { return Instant.now().toString(); }

    private static void log(String msg) {
        System.out.println(nowTs() + " - INFO - [sample-java-app] " + msg);
    }

    private static void printSampleCommands() {
        System.out.printf("""

                Sample commands (server running on port %d):

                  curl http://127.0.0.1:%d/health

                  curl -X POST http://127.0.0.1:%d%s/ready

                  curl -X POST http://127.0.0.1:%d%s/launch \\
                    -H 'Content-Type: application/json' \\
                    -d '{"microVmId": "hello_world", "meshIpv6Address": "::1"}'

                  curl -X POST http://127.0.0.1:%d%s/resume
                  curl -X POST http://127.0.0.1:%d%s/suspend
                  curl -X POST http://127.0.0.1:%d%s/terminate

                  curl -X POST http://127.0.0.1:%d/execute \\
                    -H 'Content-Type: application/json' \\
                    -d '{"code": "System.out.println(1 + 1);"}'

                """,
                PORT, PORT,
                PORT, BASE_PATH,
                PORT, BASE_PATH,
                PORT, BASE_PATH,
                PORT, BASE_PATH,
                PORT, BASE_PATH,
                PORT);
    }
}
