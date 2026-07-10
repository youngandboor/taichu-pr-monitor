package fun.taichu.prmonitor;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class GiteaApiClient {
    interface CookieProvider {
        String cookieHeader();
    }

    static final class AuthRequiredException extends IOException {
        AuthRequiredException(String message) {
            super(message);
        }
    }

    private final CookieProvider cookieProvider;

    GiteaApiClient(CookieProvider cookieProvider) {
        this.cookieProvider = cookieProvider;
    }

    boolean hasValidLogin() throws IOException, JSONException {
        requestJsonObject("/user");
        return true;
    }

    PrBriefModels.Summary fetchSummary(int prNumber) throws IOException, JSONException {
        JSONObject pr = requestJsonObject(
                "/repos/" + GiteaConfig.OWNER + "/" + GiteaConfig.REPO + "/pulls/" + prNumber);
        String headSha = pr.optJSONObject("head") == null
                ? ""
                : pr.optJSONObject("head").optString("sha", "");
        if (headSha.trim().isEmpty()) {
            throw new IOException("PR response has no head sha");
        }

        JSONArray statuses;
        try {
            statuses = requestJsonArrayPages(
                    "/repos/" + GiteaConfig.OWNER + "/" + GiteaConfig.REPO + "/statuses/" + headSha,
                    5);
        } catch (AuthRequiredException error) {
            throw error;
        } catch (IOException | JSONException error) {
            statuses = new JSONArray();
        }
        if (statuses.length() == 0) {
            JSONObject combined = requestJsonObject(
                    "/repos/" + GiteaConfig.OWNER + "/" + GiteaConfig.REPO + "/commits/" + headSha + "/status");
            statuses = combined.optJSONArray("statuses");
            if (statuses == null) {
                statuses = new JSONArray();
            }
        }

        JSONArray comments = requestJsonArrayPages(
                "/repos/" + GiteaConfig.OWNER + "/" + GiteaConfig.REPO + "/issues/" + prNumber + "/comments",
                3);
        return PrBriefLogic.buildSummary(prNumber, pr, statuses, comments);
    }

    private JSONObject requestJsonObject(String path) throws IOException, JSONException {
        return new JSONObject(request(path));
    }

    private JSONArray requestJsonArrayPages(String path, int maxPages) throws IOException, JSONException {
        JSONArray all = new JSONArray();
        for (int page = 1; page <= maxPages; page++) {
            String separator = path.contains("?") ? "&" : "?";
            JSONArray payload = new JSONArray(request(path + separator + "limit=100&page=" + page));
            for (int i = 0; i < payload.length(); i++) {
                all.put(payload.get(i));
            }
            if (payload.length() < 100) {
                break;
            }
        }
        return all;
    }

    private String request(String path) throws IOException {
        URL url = new URL(GiteaConfig.API_BASE + path);
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("GET");
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(20000);
        connection.setRequestProperty("Accept", "application/json");
        String cookies = cookieProvider.cookieHeader();
        if (cookies != null && !cookies.trim().isEmpty()) {
            connection.setRequestProperty("Cookie", cookies);
        }

        int code = connection.getResponseCode();
        InputStream stream = code >= 400 ? connection.getErrorStream() : connection.getInputStream();
        String body = readBody(stream);
        connection.disconnect();

        if (code == HttpURLConnection.HTTP_UNAUTHORIZED || code == HttpURLConnection.HTTP_FORBIDDEN) {
            throw new AuthRequiredException("Gitea login is required");
        }
        if (code >= 400) {
            throw new IOException("Gitea API " + code + ": " + body);
        }
        return body;
    }

    private static String readBody(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        StringBuilder builder = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                builder.append(line).append('\n');
            }
        }
        return builder.toString();
    }
}
