import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.HashMap;
import java.util.Map;
import okhttp3.*;

public class TestClient {

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.out.println("Usage: java TestClient <name> [ip] [port]");
            System.out.println("Example: java TestClient team1");
            System.out.println("Example: java TestClient team1 192.168.1.10 3030");
            System.exit(1);
        }

        String name = args[0];
        String ip = args.length > 1 ? args[1] : "localhost";
        String port = args.length > 2 ? args[2] : "3030";
        String baseUrl = "http://" + ip + ":" + port;

        OkHttpClient http = new OkHttpClient();
        ObjectMapper mapper = new ObjectMapper();

        Map<String, String> payload = new HashMap<>();
        payload.put("name", name);
        RequestBody body = RequestBody.create(
            mapper.writeValueAsString(payload),
            MediaType.get("application/json; charset=utf-8")
        );

        Request request = new Request.Builder()
            .url(baseUrl + "/connect")
            .post(body)
            .build();

        System.out.println("Connecting as '" + name + "' to " + baseUrl + "...");
        try (Response response = http.newCall(request).execute()) {
            if (response.code() == 200) {
                String responseBody = response.body() != null ? response.body().string() : "";
                System.out.println("Connected successfully! Response: " + responseBody);
            } else {
                System.out.println("Unexpected status: " + response.code());
            }
        }

        http.dispatcher().executorService().shutdown();
        http.connectionPool().evictAll();
        System.exit(0);
    }
}
