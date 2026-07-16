package fun.taichu.prmonitor;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String API_BASE = "https://taichu.fun/gitea/api/v1";
    private static final String WEB_BASE = "https://taichu.fun/gitea";
    private static final String NOTIFICATION_CHANNEL_ID = "ci_failures";
    private static final int NOTIFICATION_PERMISSION_REQUEST = 1001;
    private static final String OAUTH_APP_SETTINGS_URL = WEB_BASE + "/user/settings/applications";
    private static final String OAUTH_AUTHORIZE_URL = WEB_BASE + "/login/oauth/authorize";
    private static final String OAUTH_TOKEN_URL = WEB_BASE + "/login/oauth/access_token";
    private static final String REDIRECT_URI = "http://127.0.0.1:43122/oauth";
    // taichu.fun currently advertises only OIDC scopes in discovery; granular
    // Gitea API scopes are rejected by its OAuth authorize endpoint.
    private static final String OAUTH_SCOPE = "openid profile email";
    private static final String OWNER = "SystemAgentDev";
    private static final String REPO = "TaiChu";
    private static final long REFRESH_INTERVAL_MS = 60_000L;
    private static final String[] REQUIRED_GATES = {
            "protected-file-approval",
            "taichu/codex-pr-review",
            "taichu/codex-pr-test-review",
            "taichu/pr-build",
            "taichu/dev-cloud-preflight",
            "ci/merge-gate"
    };
    private static final int DEFAULT_PR_NUMBER = 1;
    private static final Set<String> PRECONDITION_GATES = new HashSet<>();
    private static final Set<String> OPTIONAL_PRECONDITION_GATES = new HashSet<>();
    static {
        PRECONDITION_GATES.add("protected-file-approval");
        PRECONDITION_GATES.add("taichu/codex-pr-review");
        PRECONDITION_GATES.add("taichu/codex-pr-test-review");
        PRECONDITION_GATES.add("taichu/pr-build");
        OPTIONAL_PRECONDITION_GATES.add("taichu/codex-pr-test-review");
    }

    private static final String STORE_NAME = "pr_monitor_auth";
    private static final String KEY_CLIENT_ID = "oauth_client_id";
    private static final String KEY_CLIENT_SECRET = "oauth_client_secret";
    private static final String KEY_ACCESS_TOKEN = "oauth_access_token";
    private static final String KEY_MONITOR_PR_NUMBER = "monitor_pr_number";
    private static final String KEY_MONITOR_ENABLED = "monitor_enabled";
    private static final String KEY_OBSERVED_COMMAND_PREFIX = "observed_ci_command_";
    private static final String KEY_NOTIFIED_FAILURES_PREFIX = "notified_ci_failures_";
    private static final String KEY_TRACKER_INITIALIZED_PREFIX = "ci_tracker_initialized_";
    private static final String KEY_TRACKER_LAST_SCANNED_PREFIX = "ci_tracker_last_scanned_";

    private static final String COLOR_BG = "#F7F8FA";
    private static final String COLOR_INK = "#111827";
    private static final String COLOR_TEXT = "#273142";
    private static final String COLOR_MUTED = "#697586";
    private static final String COLOR_PRIMARY = "#0F766E";
    private static final String COLOR_DANGER = "#B42318";
    private static final String COLOR_WARNING = "#9A6700";
    private static final String COLOR_SUCCESS = "#14733F";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SecureRandom random = new SecureRandom();

    private SharedPreferences prefs;
    private String clientId = "";
    private String clientSecret = "";
    private String accessToken = "";
    private String oauthState = "";
    private String oauthVerifier = "";
    private WebView webView;
    private boolean autoCreateOAuth = false;
    private boolean autoCreateSubmitted = false;
    private int autoCreateAttempts = 0;
    private boolean autoCreateToken = false;
    private boolean autoCreateTokenSubmitted = false;
    private int autoCreateTokenAttempts = 0;
    private int authGeneration = 0;
    private boolean loading = false;
    private boolean postingCommand = false;
    private boolean monitoring = false;
    private int prNumber = DEFAULT_PR_NUMBER;

    private LinearLayout root;
    private EditText prInput;
    private TextView titleView;
    private TextView metaView;
    private TextView statusView;
    private LinearLayout contentList;
    private Runnable monitorRunnable;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(STORE_NAME, MODE_PRIVATE);
        clientId = prefs.getString(KEY_CLIENT_ID, "");
        clientSecret = prefs.getString(KEY_CLIENT_SECRET, "");
        accessToken = prefs.getString(KEY_ACCESS_TOKEN, "");
        prNumber = prefs.getInt(KEY_MONITOR_PR_NUMBER, DEFAULT_PR_NUMBER);
        monitoring = MonitorLifecyclePolicy.enabledOnLaunch(
                !accessToken.trim().isEmpty(),
                prefs.contains(KEY_MONITOR_ENABLED),
                prefs.getBoolean(KEY_MONITOR_ENABLED, false));
        CookieManager.getInstance().setAcceptCookie(true);
        setupNotificationChannel();
        ensureNotificationPermission();

        if (!accessToken.trim().isEmpty()) {
            showMonitor();
            ensureMonitorServiceState();
            loadDefaultPrAndRefresh();
        } else {
            showSetup("");
        }
    }

    @Override
    protected void onDestroy() {
        clearMonitorTimer();
        executor.shutdownNow();
        super.onDestroy();
    }

    @Override
    protected void onPause() {
        super.onPause();
        ensureMonitorServiceState();
    }

    @Override
    protected void onResume() {
        super.onResume();
        ensureMonitorServiceState();
    }

    private void setupNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                "CI 失败提醒",
                NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("ci build / ci merge 失败时弹出横幅摘要");
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }

    private void ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION_REQUEST);
        }
    }

    private void setRoot(LinearLayout layout) {
        root = layout;
        setContentView(root);
    }

    private LinearLayout baseRoot() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(16), statusBarHeight() + dp(16), dp(16), dp(16));
        layout.setBackgroundColor(Color.parseColor(COLOR_BG));
        layout.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        return layout;
    }

    private void showSetup(String message) {
        LinearLayout layout = baseRoot();
        layout.addView(label("授权 Gitea", 24, COLOR_INK, true));
        layout.addView(spacer(6));
        layout.addView(label("复用网页登录态，一键生成访问 token 后进入监控。", 14, COLOR_MUTED, false));
        if (!message.isEmpty()) {
            layout.addView(spacer(8));
            layout.addView(label(message, 14, COLOR_DANGER, false));
        }
        layout.addView(spacer(18));
        Button token = primaryButton("一键授权并验证");
        token.setOnClickListener(v -> startAutoCreateAccessToken());
        layout.addView(token, matchFixed(52));

        TextView note = label("会在 Gitea 设置页创建 TaiChu PR Monitor token，并立即调用 /api/v1/user 校验。", 13, COLOR_MUTED, false);
        LinearLayout.LayoutParams noteParams = matchWrap();
        noteParams.topMargin = dp(12);
        layout.addView(note, noteParams);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams buttonsParams = matchWrap();
        buttonsParams.topMargin = dp(18);
        Button login = outlineButton("打开 Gitea");
        login.setOnClickListener(v -> showGiteaLogin());
        buttons.addView(login, weightFixed(1, 44));
        Button oauth = outlineButton("OAuth 备用");
        LinearLayout.LayoutParams oauthParams = weightFixed(1, 44);
        oauthParams.leftMargin = dp(8);
        buttons.addView(oauth, oauthParams);
        oauth.setOnClickListener(v -> showOAuthSetup(""));
        layout.addView(buttons, buttonsParams);
        setRoot(layout);
    }

    private void showOAuthSetup(String message) {
        LinearLayout layout = baseRoot();
        layout.addView(label("配置 OAuth", 24, COLOR_INK, true));
        layout.addView(spacer(6));
        layout.addView(label("在 Gitea 创建 OAuth2 应用，填入 client_id 后授权。", 14, COLOR_MUTED, false));
        if (!message.isEmpty()) {
            layout.addView(spacer(8));
            layout.addView(label(message, 14, COLOR_DANGER, false));
        }
        layout.addView(spacer(18));
        layout.addView(label("Redirect URI", 13, COLOR_TEXT, true));
        layout.addView(spacer(6));
        TextView redirect = label(REDIRECT_URI, 14, COLOR_TEXT, false);
        redirect.setPadding(dp(12), dp(12), dp(12), dp(12));
        redirect.setBackgroundResource(R.drawable.input_bg);
        layout.addView(redirect, matchWrap());

        EditText clientInput = new EditText(this);
        clientInput.setText(clientId);
        clientInput.setHint("OAuth client_id");
        clientInput.setSingleLine(true);
        clientInput.setTextSize(15);
        clientInput.setPadding(dp(12), 0, dp(12), 0);
        clientInput.setBackgroundResource(R.drawable.input_bg);
        LinearLayout.LayoutParams inputParams = matchFixed(56);
        inputParams.topMargin = dp(12);
        layout.addView(clientInput, inputParams);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams buttonsParams = matchWrap();
        buttonsParams.topMargin = dp(12);
        Button create = outlineButton("一键创建");
        create.setOnClickListener(v -> {
            clientId = clientInput.getText().toString().trim();
            startAutoCreateOAuthApp();
        });
        buttons.addView(create, weightFixed(1, 52));
        Button auth = primaryButton("授权");
        LinearLayout.LayoutParams authParams = weightFixed(1, 52);
        authParams.leftMargin = dp(8);
        buttons.addView(auth, authParams);
        auth.setOnClickListener(v -> {
            clientId = clientInput.getText().toString().trim();
            saveClientAndStartOAuth();
        });
        layout.addView(buttons, buttonsParams);

        TextView note = label("taichu.fun 当前 OAuth 可能返回 invalid_request；推荐优先使用 token 授权。", 13, COLOR_MUTED, false);
        LinearLayout.LayoutParams noteParams = matchWrap();
        noteParams.topMargin = dp(12);
        layout.addView(note, noteParams);
        setRoot(layout);
    }

    private void showGiteaLogin() {
        LinearLayout layout = baseRoot();
        layout.addView(label("登录 Gitea", 24, COLOR_INK, true));
        layout.addView(spacer(6));
        layout.addView(label("登录成功后返回并点“一键授权并验证”。", 14, COLOR_MUTED, false));
        Button back = primaryButton("返回授权");
        LinearLayout.LayoutParams backParams = matchFixed(52);
        backParams.topMargin = dp(12);
        backParams.bottomMargin = dp(12);
        layout.addView(back, backParams);
        back.setOnClickListener(v -> showSetup(""));
        attachWebView(layout, WEB_BASE + "/user/login");
        setRoot(layout);
    }

    private void showOAuth(String message) {
        LinearLayout layout = baseRoot();
        layout.addView(label("授权 Gitea", 24, COLOR_INK, true));
        layout.addView(spacer(6));
        layout.addView(label("登录并授权后会自动回到监控页。", 14, COLOR_MUTED, false));
        if (!message.isEmpty()) {
            layout.addView(spacer(8));
            layout.addView(label(message, 14, message.contains("失败") || message.contains("需要") ? COLOR_DANGER : COLOR_MUTED, false));
        }
        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams buttonsParams = matchWrap();
        buttonsParams.topMargin = dp(12);
        buttonsParams.bottomMargin = dp(12);
        Button reauth = primaryButton("重新授权");
        reauth.setOnClickListener(v -> startOAuth());
        buttons.addView(reauth, weightFixed(1, 52));
        Button change = outlineButton("改 client_id");
        LinearLayout.LayoutParams changeParams = weightFixed(1, 52);
        changeParams.leftMargin = dp(8);
        buttons.addView(change, changeParams);
        change.setOnClickListener(v -> resetOAuthSetup(message));
        layout.addView(buttons, buttonsParams);
        attachWebView(layout, WEB_BASE);
        setRoot(layout);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void attachWebView(LinearLayout layout, String initialUrl) {
        webView = new WebView(this);
        webView.getSettings().setJavaScriptEnabled(true);
        webView.getSettings().setDomStorageEnabled(true);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleOAuthUrl(request.getUrl().toString());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleOAuthUrl(url);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();
                handleOAuthUrl(url);
                maybeContinueAutoCreate();
                maybeContinueAutoCreateToken();
            }
        });
        layout.addView(webView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        webView.loadUrl(initialUrl);
    }

    private void startAutoCreateOAuthApp() {
        resetOAuthState();
        autoCreateOAuth = true;
        autoCreateSubmitted = false;
        autoCreateAttempts = 0;
        LinearLayout layout = baseRoot();
        layout.addView(label("创建 OAuth2 应用", 24, COLOR_INK, true));
        layout.addView(spacer(6));
        layout.addView(label("正在自动创建 OAuth2 应用，创建后会自动进入授权页。", 14, COLOR_MUTED, false));
        Button back = primaryButton("返回填写 client_id");
        LinearLayout.LayoutParams backParams = matchFixed(52);
        backParams.topMargin = dp(12);
        backParams.bottomMargin = dp(12);
        layout.addView(back, backParams);
        back.setOnClickListener(v -> {
            autoCreateOAuth = false;
            showSetup("");
        });
        attachWebView(layout, OAUTH_APP_SETTINGS_URL);
        setRoot(layout);
        main.postDelayed(this::runAutoCreateStep, 2500);
    }

    private void startAutoCreateAccessToken() {
        resetOAuthState();
        autoCreateToken = true;
        autoCreateTokenSubmitted = false;
        autoCreateTokenAttempts = 0;
        LinearLayout layout = baseRoot();
        layout.addView(label("创建访问 token", 24, COLOR_INK, true));
        layout.addView(spacer(6));
        layout.addView(label("正在 Gitea 设置页创建 token，完成后会自动验证。", 14, COLOR_MUTED, false));
        Button back = primaryButton("返回授权页");
        LinearLayout.LayoutParams backParams = matchFixed(52);
        backParams.topMargin = dp(12);
        backParams.bottomMargin = dp(12);
        layout.addView(back, backParams);
        back.setOnClickListener(v -> {
            autoCreateToken = false;
            showSetup("");
        });
        attachWebView(layout, OAUTH_APP_SETTINGS_URL);
        setRoot(layout);
        main.postDelayed(this::runAutoCreateTokenStep, 2500);
    }

    private void maybeContinueAutoCreate() {
        if (!autoCreateOAuth) {
            return;
        }
        autoCreateAttempts++;
        if (autoCreateAttempts > 10) {
            autoCreateOAuth = false;
            Toast.makeText(this, "自动创建超时，请手动创建后填入 client_id", Toast.LENGTH_LONG).show();
            return;
        }
        main.postDelayed(this::runAutoCreateStep, 800);
    }

    private void maybeContinueAutoCreateToken() {
        if (!autoCreateToken) {
            return;
        }
        autoCreateTokenAttempts++;
        if (autoCreateTokenAttempts > 16) {
            autoCreateToken = false;
            showSetup("自动创建 token 超时；请确认已登录 Gitea 后重试。");
            return;
        }
        main.postDelayed(this::runAutoCreateTokenStep, 900);
    }

    private void runAutoCreateStep() {
        if (!autoCreateOAuth || webView == null) {
            return;
        }
        webView.evaluateJavascript(autoCreateScript(autoCreateSubmitted), result -> {
            String message = unquoteJsResult(result);
            if (message.startsWith("client_id:")) {
                clientId = message.substring("client_id:".length()).trim();
                clientSecret = "";
                autoCreateOAuth = false;
                prefs.edit().putString(KEY_CLIENT_ID, clientId).putString(KEY_CLIENT_SECRET, "").putString(KEY_ACCESS_TOKEN, "").apply();
                accessToken = "";
                showOAuth("");
                startOAuth();
            } else if (message.startsWith("client_credentials:")) {
                String[] parts = message.substring("client_credentials:".length()).split("\\n", 2);
                clientId = parts.length > 0 ? parts[0].trim() : "";
                clientSecret = parts.length > 1 ? parts[1].trim() : "";
                autoCreateOAuth = false;
                prefs.edit().putString(KEY_CLIENT_ID, clientId).putString(KEY_CLIENT_SECRET, clientSecret).putString(KEY_ACCESS_TOKEN, "").apply();
                accessToken = "";
                showOAuth("");
                startOAuth();
            } else if ("submitted".equals(message) || "public_updated".equals(message)) {
                autoCreateSubmitted = true;
                main.postDelayed(this::runAutoCreateStep, 1300);
            }
        });
    }

    private void runAutoCreateTokenStep() {
        if (!autoCreateToken || webView == null) {
            return;
        }
        webView.evaluateJavascript(autoCreateTokenScript(autoCreateTokenSubmitted), result -> {
            String message = unquoteJsResult(result);
            if (message.startsWith("token:")) {
                autoCreateToken = false;
                finishGeneratedToken(message.substring("token:".length()).trim());
            } else if ("submitted".equals(message)) {
                autoCreateTokenSubmitted = true;
                main.postDelayed(this::runAutoCreateTokenStep, 1400);
            }
        });
    }

    private String autoCreateTokenScript(boolean submitted) {
        String tokenName = "TaiChu PR Monitor " + new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.ROOT).format(new Date());
        return "(() => {"
                + "const tokenRe=/(gitea_[A-Za-z0-9_-]{20,}|[a-f0-9]{40,}|[A-Za-z0-9_-]{48,})/;"
                + "const text=()=>document.body?document.body.innerText:'';"
                + "const findToken=()=>{"
                + "const nodes=Array.from(document.querySelectorAll('input, textarea, code, pre, kbd, .flash, .message, .ui.message, .segment, .field, td'));"
                + "for(const e of nodes){const v=(e.value||e.innerText||'').trim();const ctx=((e.closest('.flash,.message,.ui.message,.segment,.field,form,tr')||e.parentElement||document.body).innerText||'');"
                + "if(/csrf/i.test((e.name||'')+' '+(e.id||'')+' '+ctx))continue;"
                + "if(/token|令牌|access|密钥|secret/i.test(ctx)){const m=v.match(tokenRe)||ctx.match(tokenRe);if(m)return m[1];}}"
                + "const m=text().match(tokenRe);return m?m[1]:'';};"
                + "if(" + submitted + "){const token=findToken();if(token)return 'token:'+token;return 'waiting';}"
                + "for(const item of Array.from(document.querySelectorAll('details'))){if(/token|令牌|application|应用/i.test(item.innerText||''))item.open=true;}"
                + "const forms=Array.from(document.querySelectorAll('form'));"
                + "let form=forms.find(f=>{const s=((f.getAttribute('action')||'')+' '+(f.innerText||'')).toLowerCase();return (s.includes('token')||s.includes('令牌')||s.includes('access'))&&!s.includes('oauth2');});"
                + "if(!form)form=forms.find(f=>{const s=(f.innerText||'').toLowerCase();return (s.includes('generate')||s.includes('创建')||s.includes('生成'))&&(s.includes('token')||s.includes('令牌'));});"
                + "if(!form){window.scrollBy(0,Math.floor(window.innerHeight*0.75));return 'waiting';}"
                + "const fields=Array.from(form.querySelectorAll('input[type=text], input:not([type])'));"
                + "const name=fields.find(e=>!/(csrf|token|scope|redirect|client)/i.test((e.name||'')+' '+(e.id||'')))||fields[0];"
                + "if(name){name.value=" + jsString(tokenName) + ";name.dispatchEvent(new Event('input',{bubbles:true}));name.dispatchEvent(new Event('change',{bubbles:true}));}"
                + "for(const c of Array.from(form.querySelectorAll('input[type=checkbox]'))){if(!c.checked)c.click();c.checked=true;c.dispatchEvent(new Event('change',{bubbles:true}));}"
                + "for(const row of Array.from(form.querySelectorAll('tr,.field,.inline.field'))){const s=(row.innerText||'').toLowerCase();if(!/(issue|repository|user)/.test(s))continue;const radios=Array.from(row.querySelectorAll('input[type=radio]'));if(radios.length){const target=radios[radios.length-1];if(!target.checked)target.click();target.checked=true;target.dispatchEvent(new Event('change',{bubbles:true}));}}"
                + "for(const s of Array.from(form.querySelectorAll('select'))){for(const o of Array.from(s.options)){if(/write|all|repo|issue|user/i.test(o.value+' '+o.text)){s.value=o.value;s.dispatchEvent(new Event('change',{bubbles:true}));break;}}}"
                + "const submit=form.querySelector('button[type=submit], input[type=submit], button.primary, button');"
                + "if(!submit){window.scrollBy(0,Math.floor(window.innerHeight*0.75));return 'waiting';}submit.scrollIntoView({block:'center'});submit.click();return 'submitted';"
                + "})()";
    }

    private String autoCreateScript(boolean submitted) {
        String appName = "TaiChu PR Monitor " + new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.ROOT).format(new Date());
        return "(() => {"
                + "const text=document.body?document.body.innerText:'';"
                + "if(" + submitted + "){"
                + "const idMatch=text.match(/Client\\s*ID\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i)||text.match(/客户端\\s*ID\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i)||text.match(/client_id\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i);"
                + "const secretMatch=text.match(/Client\\s*Secret\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i)||text.match(/客户端\\s*密钥\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i)||text.match(/client_secret\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i);"
                + "if(idMatch&&secretMatch)return 'client_credentials:'+idMatch[1]+'\\n'+secretMatch[1];"
                + "const inputs=Array.from(document.querySelectorAll('input, textarea'));"
                + "let id='';let secret='';"
                + "for(const e of inputs){const v=e.value||'';if(/^[A-Za-z0-9._~-]{20,}$/.test(v)){const l=((e.closest('.field')||e.parentElement||document.body).innerText||'')+' '+(e.getAttribute('name')||'')+' '+(e.id||'');if(/Client\\s*ID|客户端\\s*ID|client_id/i.test(l))id=v;if(/Client\\s*Secret|客户端\\s*密钥|client_secret/i.test(l))secret=v;}}"
                + "if(id&&secret)return 'client_credentials:'+id+'\\n'+secret;"
                + "const boxes=Array.from(document.querySelectorAll('input[type=checkbox]'));"
                + "let changed=false;let changedForm=null;"
                + "for(const c of boxes){if(c.checked){c.click();c.checked=false;c.dispatchEvent(new Event('input',{bubbles:true}));c.dispatchEvent(new Event('change',{bubbles:true}));changed=true;changedForm=c.closest('form')||changedForm;}}"
                + "if(changed){const submit=(changedForm&&changedForm.querySelector('button[type=submit], input[type=submit], button.primary, button'))||document.querySelector('button[type=submit], input[type=submit], button.primary, button');if(submit)submit.click();return 'public_updated';}"
                + "if(idMatch)return 'client_id:'+idMatch[1];"
                + "if(id)return 'client_id:'+id;"
                + "return 'waiting';"
                + "}"
                + "for(const item of Array.from(document.querySelectorAll('details'))){if((item.innerText||'').includes('OAuth2'))item.open=true;}"
                + "const forms=Array.from(document.querySelectorAll('form'));"
                + "const form=forms.find(f=>(((f.getAttribute('action')||'')+' '+(f.innerText||'')).toLowerCase().includes('oauth2')&&f.querySelector('textarea')));"
                + "if(!form)return 'waiting';"
                + "const fields=Array.from(form.querySelectorAll('input[type=text], input:not([type]), textarea'));"
                + "const name=fields.find(e=>e.tagName.toLowerCase()==='input');"
                + "const redirect=fields.find(e=>e.tagName.toLowerCase()==='textarea')||fields.find(e=>(e.getAttribute('name')||'').toLowerCase().includes('redirect'));"
                + "if(!name||!redirect)return 'waiting';"
                + "name.value=" + jsString(appName) + ";name.dispatchEvent(new Event('input',{bubbles:true}));name.dispatchEvent(new Event('change',{bubbles:true}));"
                + "redirect.value=" + jsString(REDIRECT_URI) + ";redirect.dispatchEvent(new Event('input',{bubbles:true}));redirect.dispatchEvent(new Event('change',{bubbles:true}));"
                + "for(const c of Array.from(form.querySelectorAll('input[type=checkbox]'))){if(c.checked)c.click();c.checked=false;c.dispatchEvent(new Event('change',{bubbles:true}));}"
                + "const submit=form.querySelector('button[type=submit], input[type=submit], button.primary, button');"
                + "if(!submit)return 'waiting';submit.click();return 'submitted';"
                + "})()";
    }

    private void saveClientAndStartOAuth() {
        if (clientId.trim().isEmpty()) {
            showSetup("请先填入 OAuth client_id。");
            return;
        }
        clientSecret = "";
        prefs.edit().putString(KEY_CLIENT_ID, clientId.trim()).putString(KEY_CLIENT_SECRET, "").putString(KEY_ACCESS_TOKEN, "").apply();
        accessToken = "";
        showOAuth("");
        startOAuth();
    }

    private void startOAuth() {
        if (clientId.trim().isEmpty()) {
            showSetup("请先填入 OAuth client_id。");
            return;
        }
        oauthState = randomToken(24);
        oauthVerifier = clientSecret.trim().isEmpty() ? randomToken(48) : "";
        Uri.Builder builder = Uri.parse(OAUTH_AUTHORIZE_URL).buildUpon()
                .appendQueryParameter("client_id", clientId)
                .appendQueryParameter("redirect_uri", REDIRECT_URI)
                .appendQueryParameter("response_type", "code")
                .appendQueryParameter("state", oauthState);
        if (!OAUTH_SCOPE.trim().isEmpty()) {
            builder.appendQueryParameter("scope", OAUTH_SCOPE);
        }
        if (clientSecret.trim().isEmpty()) {
            builder.appendQueryParameter("code_challenge", oauthVerifier)
                    .appendQueryParameter("code_challenge_method", "plain");
        }
        Uri uri = builder.build();
        if (webView != null) {
            webView.loadUrl(uri.toString());
        }
    }

    private boolean handleOAuthUrl(String url) {
        if (url == null || !url.startsWith(REDIRECT_URI)) {
            return false;
        }
        Uri uri = Uri.parse(url);
        String error = valueOrEmpty(uri.getQueryParameter("error"));
        if (!error.isEmpty()) {
            String message = "OAuth 授权失败：" + error;
            if ("server_error".equals(error)) {
                message = "OAuth 授权失败：server_error；如果刚升级权限，请返回配置页点“一键创建”重建 client_id。";
            }
            showOAuth(message);
            return true;
        }
        String code = valueOrEmpty(uri.getQueryParameter("code"));
        String state = valueOrEmpty(uri.getQueryParameter("state"));
        if (code.isEmpty()) {
            showOAuth("OAuth 回调缺少 code。");
            return true;
        }
        if (!oauthState.equals(state)) {
            showOAuth("OAuth state 不匹配，请重新授权。");
            return true;
        }
        finishOAuth(code);
        return true;
    }

    private void finishOAuth(String code) {
        setLoading(true, "正在换取 access token…");
        executor.execute(() -> {
            try {
                String body = form("grant_type", "authorization_code")
                        + "&" + form("client_id", clientId)
                        + "&" + form("code", code)
                        + "&" + form("redirect_uri", REDIRECT_URI);
                if (clientSecret.trim().isEmpty()) {
                    body += "&" + form("code_verifier", oauthVerifier);
                }
                if (!clientSecret.trim().isEmpty()) {
                    body += "&" + form("client_secret", clientSecret.trim());
                }
                String text = requestRaw(OAUTH_TOKEN_URL, "POST", body, headers("Accept", "application/json", "Content-Type", "application/x-www-form-urlencoded"));
                JSONObject json = new JSONObject(text);
                String token = json.optString("access_token", "");
                if (token.isEmpty()) {
                    throw new IOException("OAuth token response has no access_token");
                }
                accessToken = token;
                validateAccessToken();
                monitoring = true;
                prefs.edit()
                        .putString(KEY_CLIENT_ID, clientId)
                        .putString(KEY_CLIENT_SECRET, clientSecret)
                        .putString(KEY_ACCESS_TOKEN, accessToken)
                        .putBoolean(KEY_MONITOR_ENABLED, true)
                        .apply();
                main.post(() -> {
                    showMonitor();
                    ensureMonitorServiceState();
                    loadDefaultPrAndRefresh();
                });
            } catch (Exception error) {
                main.post(() -> {
                    String message = "换取 access token 失败：" + error.getMessage();
                    if (message.contains("unauthorized_client")) {
                        message += "；请点“改 client_id”后重新“一键创建”，旧 OAuth app 可能仍是机密客户端。";
                    }
                    showOAuth(message);
                });
            } finally {
                main.post(() -> setLoading(false, ""));
            }
        });
    }

    private void showMonitor() {
        LinearLayout layout = baseRoot();
        layout.setPadding(dp(16), statusBarHeight() + dp(14), dp(16), 0);
        titleView = titleLabel("PR #" + prNumber);
        metaView = label("只看关键门禁、队列和 PR body", 12, COLOR_MUTED, false);
        statusView = label("等待刷新", 13, COLOR_MUTED, false);
        layout.addView(headerBar());
        layout.addView(spacer(6));
        layout.addView(titleView);
        layout.addView(spacer(8));
        layout.addView(metaView);
        layout.addView(spacer(12));
        layout.addView(commandBar());
        layout.addView(spacer(6));
        layout.addView(ciCommandBar());
        layout.addView(spacer(10));
        layout.addView(statusView);
        layout.addView(spacer(10));

        ScrollView scroll = new ScrollView(this);
        contentList = new LinearLayout(this);
        contentList.setOrientation(LinearLayout.VERTICAL);
        contentList.setPadding(0, 0, 0, dp(32));
        scroll.addView(contentList);
        layout.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setRoot(layout);
    }

    private View headerBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        TextView appName = label("TAICHU PR MONITOR", 11, "#8B95A5", true);
        bar.addView(appName, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView logout = label("退出", 12, COLOR_MUTED, true);
        logout.setGravity(Gravity.CENTER);
        logout.setPadding(dp(10), dp(4), dp(10), dp(4));
        logout.setOnClickListener(v -> logoutFromApp());
        bar.addView(logout, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(30)));
        return bar;
    }

    private View commandBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setPadding(dp(3), dp(3), dp(3), dp(3));
        bar.setBackgroundResource(R.drawable.card_white);

        prInput = new EditText(this);
        prInput.setText(String.valueOf(prNumber));
        prInput.setInputType(InputType.TYPE_CLASS_NUMBER);
        prInput.setSingleLine(true);
        prInput.setTextSize(14);
        prInput.setTextColor(Color.parseColor(COLOR_INK));
        prInput.setIncludeFontPadding(false);
        prInput.setPadding(dp(12), 0, dp(12), 0);
        prInput.setBackgroundColor(Color.rgb(247, 248, 250));
        bar.addView(prInput, weightFixed(1, 40));

        Button refresh = outlineButton("刷新");
        LinearLayout.LayoutParams refreshParams = fixed(66, 40);
        refreshParams.leftMargin = dp(6);
        bar.addView(refresh, refreshParams);
        refresh.setOnClickListener(v -> refreshSummary(true));

        Button monitor = primaryButton("监控");
        monitor.setText(monitoring ? "暂停" : "监控");
        LinearLayout.LayoutParams monitorParams = fixed(68, 40);
        monitorParams.leftMargin = dp(6);
        bar.addView(monitor, monitorParams);
        monitor.setOnClickListener(v -> {
            monitoring = !monitoring;
            monitor.setText(monitoring ? "暂停" : "监控");
            if (monitoring) {
                persistMonitorState(true);
                ensureMonitorServiceState();
                refreshSummary(true);
            } else {
                persistMonitorState(false);
                stopService(new Intent(this, PrMonitorService.class));
                clearMonitorTimer();
                setStatus("已暂停自动刷新", COLOR_MUTED);
            }
        });
        return bar;
    }

    private View ciCommandBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        Button rebuild = outlineButton("rebuild");
        rebuild.setOnClickListener(v -> postCiCommand("/ci build"));
        bar.addView(rebuild, weightFixed(1, 40));
        Button remerge = darkButton("remerge");
        LinearLayout.LayoutParams remergeParams = weightFixed(1, 40);
        remergeParams.leftMargin = dp(8);
        bar.addView(remerge, remergeParams);
        remerge.setOnClickListener(v -> postCiCommand("/ci merge"));
        return bar;
    }

    private void refreshSummary(boolean userInitiated) {
        int number = applyPrInput();
        if (monitoring) {
            persistMonitorState(true);
        }
        setLoading(true, userInitiated ? "正在刷新 PR #" + number + "…" : "自动刷新中…");
        executor.execute(() -> {
            try {
                PrSummary summary = fetchSummary(number);
                main.post(() -> renderSummary(summary));
            } catch (AuthRequired error) {
                main.post(() -> {
                    monitoring = false;
                    persistMonitorState(false);
                    stopService(new Intent(this, PrMonitorService.class));
                    clearAccessToken();
                    showOAuth("OAuth 授权过期，请重新授权。");
                });
            } catch (Exception error) {
                main.post(() -> setStatus("刷新失败：" + error.getMessage(), COLOR_DANGER));
            } finally {
                main.post(() -> {
                    setLoading(false, "");
                    if (monitoring) {
                        scheduleMonitorTimer();
                    }
                });
            }
        });
    }

    private void loadDefaultPrAndRefresh() {
        setLoading(true, "正在查找你的最新 PR…");
        executor.execute(() -> {
            try {
                int latest = findLatestPullRequestForCurrentUser();
                main.post(() -> {
                    if (latest > 0) {
                        prNumber = latest;
                        if (prInput != null) {
                            prInput.setText(String.valueOf(prNumber));
                        }
                    }
                    refreshSummary(true);
                });
            } catch (AuthRequired error) {
                main.post(() -> {
                    monitoring = false;
                    persistMonitorState(false);
                    stopService(new Intent(this, PrMonitorService.class));
                    clearAccessToken();
                    showOAuth("授权过期，请重新授权。");
                });
            } catch (Exception error) {
                main.post(() -> {
                    setStatus("未找到你的最新 PR，使用 PR #" + prNumber + "：" + error.getMessage(), COLOR_WARNING);
                    refreshSummary(true);
                });
            }
        });
    }

    private int findLatestPullRequestForCurrentUser() throws Exception {
        JSONObject user = requestObject("/user");
        String login = user.optString("login", "");
        if (login.trim().isEmpty()) {
            throw new IOException("Gitea /user response has no login");
        }
        List<JSONObject> pulls = requestArrayPages("/repos/" + OWNER + "/" + REPO + "/pulls?state=all", 5);
        JSONObject latest = null;
        String latestTime = "";
        for (JSONObject pull : pulls) {
            JSONObject author = pull.optJSONObject("user");
            String authorLogin = author == null ? "" : author.optString("login", "");
            if (!login.equals(authorLogin)) {
                continue;
            }
            String updated = firstNonEmpty(pull.optString("updated_at", ""), pull.optString("created_at", ""));
            if (latest == null || updated.compareTo(latestTime) > 0) {
                latest = pull;
                latestTime = updated;
            }
        }
        if (latest == null) {
            throw new IOException("没有找到 " + login + " 在 TaiChu 仓库的 PR");
        }
        int number = latest.optInt("number", 0);
        if (number <= 0) {
            throw new IOException("latest PR response has no number");
        }
        return number;
    }

    private PrSummary fetchSummary(int number) throws Exception {
        JSONObject pr = requestObject("/repos/" + OWNER + "/" + REPO + "/pulls/" + number);
        JSONObject head = pr.optJSONObject("head");
        String headSha = head == null ? "" : head.optString("sha", "");
        if (headSha.isEmpty()) {
            throw new IOException("PR response has no head sha");
        }

        List<JSONObject> statuses = new ArrayList<>();
        try {
            statuses = requestArrayPages("/repos/" + OWNER + "/" + REPO + "/statuses/" + headSha, 5);
        } catch (Exception ignored) {
            statuses = new ArrayList<>();
        }
        if (statuses.isEmpty()) {
            JSONObject combined = requestObject("/repos/" + OWNER + "/" + REPO + "/commits/" + headSha + "/status");
            JSONArray arr = combined.optJSONArray("statuses");
            if (arr != null) {
                statuses = jsonArrayToList(arr);
            }
        }
        List<JSONObject> comments = requestArrayPages("/repos/" + OWNER + "/" + REPO + "/issues/" + number + "/comments", 3);
        return buildSummary(number, pr, statuses, comments);
    }

    private PrSummary buildSummary(int number, JSONObject pr, List<JSONObject> statuses, List<JSONObject> comments) {
        PrSummary summary = new PrSummary();
        summary.number = number;
        summary.title = pr.optString("title", "");
        summary.body = pr.optString("body", "");
        summary.state = pr.optString("state", "");
        JSONObject user = pr.optJSONObject("user");
        summary.author = user == null ? "" : user.optString("login", "");
        JSONObject head = pr.optJSONObject("head");
        JSONObject base = pr.optJSONObject("base");
        summary.headSha = head == null ? "" : head.optString("sha", "");
        summary.headRef = head == null ? "" : head.optString("ref", "");
        summary.baseRef = base == null ? "" : base.optString("ref", "");
        summary.fetchedAt = isoNow();

        Map<String, GateItem> latestByContext = new HashMap<>();
        for (JSONObject status : statuses) {
            String context = status.optString("context", status.optString("name", ""));
            String normalized = normalizeGateContext(context);
            if (normalized.isEmpty()) {
                continue;
            }
            GateItem item = new GateItem();
            item.context = normalized;
            String rawState = firstNonEmpty(status.optString("state", ""), status.optString("status", ""));
            item.summary = firstNonEmpty(status.optString("description", ""), rawState);
            item.state = GateStateClassifier.effectiveState(rawState, item.summary);
            item.updatedAt = firstNonEmpty(status.optString("updated_at", ""), status.optString("created_at", ""));
            item.targetUrl = status.optString("target_url", "");
            GateItem current = latestByContext.get(normalized);
            if (current == null || item.updatedAt.compareTo(current.updatedAt) >= 0) {
                latestByContext.put(normalized, item);
            }
        }

        List<QueueEvent> queue = new ArrayList<>();
        for (JSONObject comment : comments) {
            updateLatestCiCommand(summary, comment);
            String body = comment.optString("body", "");
            GateItem commentGate = gateFromComment(comment, summary.headSha);
            if (commentGate != null) {
                GateItem current = latestByContext.get(commentGate.context);
                if (current == null || commentGate.updatedAt.compareTo(current.updatedAt) >= 0) {
                    latestByContext.put(commentGate.context, commentGate);
                }
            }
            QueueEvent event = queueEventFromComment(comment, summary.headSha);
            if (event == null) {
                event = queueEventFromCiCommand(comment);
            }
            if (event != null) {
                queue.add(event);
            }
        }
        summary.queue = QueueEventSelector.latestRelevantPerKind(
                queue,
                summary.latestCiCommandAtByKind,
                new QueueEventSelector.ItemView<QueueEvent>() {
                    @Override
                    public String kind(QueueEvent item) {
                        return item.kind;
                    }

                    @Override
                    public String updatedAt(QueueEvent item) {
                        return item.updatedAt;
                    }
                },
                8);
        boolean preconditionsPassed = preconditionsPassed(latestByContext);
        for (String context : REQUIRED_GATES) {
            GateItem item = latestByContext.get(context);
            if (item == null) {
                if (OPTIONAL_PRECONDITION_GATES.contains(context)) {
                    continue;
                }
                GateItem missing = missingGate(context, summary.headSha,
                        PRECONDITION_GATES.contains(context)
                                ? "前置门禁尚未产出当前 head 结果。"
                                : (preconditionsPassed ? "前置门禁已通过，当前 head 等待该门禁运行。" : "等待前置门禁通过后运行。"));
                if (PRECONDITION_GATES.contains(context) || preconditionsPassed) {
                    summary.gates.add(missing);
                } else {
                    summary.waitingGates.add(missing);
                }
            } else if (!isSuccessfulGate(item)) {
                summary.gates.add(item);
            } else {
                summary.successGates.add(item);
            }
        }
        Collections.sort(summary.gates, (a, b) -> b.updatedAt.compareTo(a.updatedAt));
        Collections.sort(summary.successGates, (a, b) -> b.updatedAt.compareTo(a.updatedAt));
        return summary;
    }

    private void updateLatestCiCommand(PrSummary summary, JSONObject comment) {
        String command = exactCiCommand(comment.optString("body", ""));
        if (command.isEmpty()) {
            return;
        }
        String updatedAt = firstNonEmpty(comment.optString("updated_at", ""), comment.optString("created_at", ""));
        String commentId = String.valueOf(comment.optLong("id", 0L));
        if (summary.latestCiCommandKey.isEmpty() || updatedAt.compareTo(summary.latestCiCommandAt) >= 0) {
            summary.latestCiCommand = command;
            summary.latestCiCommandAt = updatedAt;
            summary.latestCiCommandKey = summary.number + ":" + command + ":" + updatedAt + ":" + commentId;
        }
        String queueKind = kindForCiCommand(command);
        String currentAt = summary.latestCiCommandAtByKind.get(queueKind);
        if (currentAt == null || updatedAt.compareTo(currentAt) >= 0) {
            summary.latestCiCommandAtByKind.put(queueKind, updatedAt);
        }
    }

    private String kindForCiCommand(String command) {
        return CiCommandQueueEvents.kindForCommand(command).toLowerCase(Locale.ROOT);
    }

    private boolean preconditionsPassed(Map<String, GateItem> latestByContext) {
        for (String context : PRECONDITION_GATES) {
            GateItem item = latestByContext.get(context);
            if (item == null && OPTIONAL_PRECONDITION_GATES.contains(context)) {
                continue;
            }
            if (item == null || !isSuccessfulGate(item)) {
                return false;
            }
        }
        return true;
    }

    private QueueEvent queueEventFromComment(JSONObject comment, String currentHeadSha) {
        String body = comment.optString("body", "");
        if (!isQueueStatusComment(body)) {
            return null;
        }
        if (referencesDifferentHead(body, currentHeadSha)) {
            return null;
        }
        if (isInactiveQueueComment(body)) {
            return null;
        }

        QueueEvent event = new QueueEvent();
        JSONObject commentUser = comment.optJSONObject("user");
        event.author = commentUser == null ? "" : commentUser.optString("login", "");
        event.createdAt = comment.optString("created_at", "");
        event.updatedAt = firstNonEmpty(comment.optString("updated_at", ""), event.createdAt);
        event.kind = queueKind(body);
        event.state = stateFromComment(body);
        if (isSuccessfulQueueEvent(event)) {
            return null;
        }
        event.summary = queueSummary(body, currentHeadSha);
        return event;
    }

    private QueueEvent queueEventFromCiCommand(JSONObject comment) {
        String command = exactCiCommand(comment.optString("body", ""));
        if (command.isEmpty()) {
            return null;
        }
        QueueEvent event = new QueueEvent();
        JSONObject commentUser = comment.optJSONObject("user");
        event.author = commentUser == null ? "" : commentUser.optString("login", "");
        event.createdAt = comment.optString("created_at", "");
        event.updatedAt = firstNonEmpty(comment.optString("updated_at", ""), event.createdAt);
        event.kind = CiCommandQueueEvents.kindForCommand(command);
        event.state = "pending";
        event.summary = CiCommandQueueEvents.summaryForCommand(command);
        return event;
    }

    private boolean isQueueStatusComment(String body) {
        String lower = valueOrEmpty(body).toLowerCase(Locale.ROOT);
        if (isInactiveQueueComment(body) || isBuildTimingComment(body)) {
            return false;
        }
        return lower.contains("merge-gate-queue-status")
                || lower.contains("pr-build-queue-status")
                || lower.contains("queue status")
                || body.contains("排队状态")
                || body.contains("入队成功")
                || body.contains("已入队")
                || body.contains("暂不能入队");
    }

    private void renderSummary(PrSummary summary) {
        titleView.setText("PR #" + summary.number + " · " + summary.title);
        metaView.setText(summary.author + " · " + summary.headRef + " -> " + summary.baseRef + " · " + summary.state);
        setStatus(summary.gates.isEmpty()
                ? "没有关键失败；成功门禁已前置显示 · " + compactTime(summary.fetchedAt)
                : "发现 " + summary.gates.size() + " 个关键非成功门禁 · " + compactTime(summary.fetchedAt), COLOR_MUTED);
        contentList.removeAllViews();
        addBlockers(summary.gates);
        addQueue(summary.successGates, summary.waitingGates, summary.queue);
        addBody(summary.body);
        processMonitorNotifications(summary);
    }

    private void processMonitorNotifications(PrSummary summary) {
        if (!monitoring) {
            return;
        }
        String observedKeyName = KEY_OBSERVED_COMMAND_PREFIX + summary.number;
        String notifiedKeyName = KEY_NOTIFIED_FAILURES_PREFIX + summary.number;
        String initializedKeyName = KEY_TRACKER_INITIALIZED_PREFIX + summary.number;
        String lastScannedKeyName = KEY_TRACKER_LAST_SCANNED_PREFIX + summary.number;
        CiFailureTracker.State current = new CiFailureTracker.State(
                prefs.getString(observedKeyName, ""),
                prefs.getStringSet(notifiedKeyName, new HashSet<>()),
                prefs.getBoolean(initializedKeyName, false),
                prefs.getString(lastScannedKeyName, ""));
        CiFailureTracker.Result result = CiFailureTracker.poll(current, notificationSnapshot(summary));
        for (CiFailureTracker.GateFailure failure : result.notifications) {
            postGateFailureNotification(summary.number, failure);
        }
        prefs.edit()
                .putString(observedKeyName, result.state.observedCommandKey)
                .putStringSet(notifiedKeyName, result.state.notifiedFailureKeys)
                .putBoolean(initializedKeyName, result.state.initialized)
                .putString(lastScannedKeyName, result.state.lastScannedAt)
                .apply();
    }

    private CiFailureTracker.Snapshot notificationSnapshot(PrSummary summary) {
        List<CiFailureTracker.GateFailure> failures = new ArrayList<>();
        for (GateItem gate : summary.gates) {
            if (isActionableFailure(gate.state, gate.summary)) {
                failures.add(new CiFailureTracker.GateFailure(gate.context, gate.updatedAt, gate.summary));
            }
        }
        return new CiFailureTracker.Snapshot(
                summary.number,
                summary.latestCiCommand,
                summary.latestCiCommandAt,
                summary.latestCiCommandKey,
                summary.fetchedAt,
                summary.baseRef,
                failures);
    }

    private boolean isActionableFailure(String state, String summary) {
        return GateStateClassifier.isActionableFailure(state, summary);
    }

    private void postGateFailureNotification(int number, CiFailureTracker.GateFailure gate) {
        String message = CiFailureTracker.notificationText(gate.summary);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION_REQUEST);
            Toast.makeText(this, gate.context + " 失败：" + message, Toast.LENGTH_LONG).show();
            return;
        }
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_error)
                .setContentTitle("PR #" + number + " " + gate.context + " 失败")
                .setContentText(message)
                .setStyle(new Notification.BigTextStyle().bigText(message))
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .setPriority(Notification.PRIORITY_HIGH)
                .setDefaults(Notification.DEFAULT_ALL)
                .build();
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(3000 + Math.abs((number + gate.context).hashCode() % 100000), notification);
        }
    }

    private void addBlockers(List<GateItem> gates) {
        LinearLayout section = section("关键失败");
        if (gates.isEmpty()) {
            TextView ok = label("关键门禁暂无失败", 15, COLOR_SUCCESS, true);
            ok.setPadding(dp(12), dp(11), dp(12), dp(11));
            ok.setBackgroundResource(R.drawable.card_green);
            section.addView(ok, matchWrap());
        } else {
            for (GateItem gate : gates) {
                LinearLayout card = new LinearLayout(this);
                card.setOrientation(LinearLayout.VERTICAL);
                card.setPadding(dp(12), dp(11), dp(12), dp(11));
                card.setBackgroundResource(R.drawable.card_red);
                LinearLayout.LayoutParams params = matchWrap();
                params.bottomMargin = dp(8);
                section.addView(card, params);
                card.addView(label(gate.context + formatState(gate.state), 14, COLOR_DANGER, true));
                card.addView(spacer(6));
                TextView detail = label(firstNonEmpty(gate.summary, gate.updatedAt), 13, COLOR_TEXT, false);
                detail.setMaxLines(4);
                detail.setEllipsize(TextUtils.TruncateAt.END);
                card.addView(detail);
            }
        }
        contentList.addView(section, sectionParams());
    }

    private void addQueue(List<GateItem> successGates, List<GateItem> waitingGates, List<QueueEvent> queue) {
        LinearLayout section = section("队列");
        if (successGates.isEmpty() && waitingGates.isEmpty() && queue.isEmpty()) {
            TextView empty = label("暂无有用排队信息", 15, COLOR_MUTED, true);
            empty.setPadding(dp(12), dp(11), dp(12), dp(11));
            empty.setBackgroundResource(R.drawable.card_gray);
            section.addView(empty, matchWrap());
        } else {
            for (GateItem gate : successGates) {
                LinearLayout card = new LinearLayout(this);
                card.setOrientation(LinearLayout.VERTICAL);
                card.setPadding(dp(12), dp(11), dp(12), dp(11));
                card.setBackgroundResource(R.drawable.card_green);
                LinearLayout.LayoutParams params = matchWrap();
                params.bottomMargin = dp(8);
                section.addView(card, params);
                card.addView(label(gate.context + " · success", 14, COLOR_SUCCESS, true));
                card.addView(spacer(5));
                card.addView(label(firstNonEmpty(gate.updatedAt, "当前 head 最新状态"), 11, COLOR_MUTED, true));
                String detailText = firstNonEmpty(gate.summary, "当前 head 该门禁已通过。");
                if (!detailText.toLowerCase(Locale.ROOT).contains("success")
                        && !detailText.contains("通过")
                        && !detailText.contains("成功")) {
                    detailText = "当前 head 该门禁已通过。";
                }
                card.addView(spacer(6));
                TextView detail = label(detailText, 13, COLOR_TEXT, false);
                detail.setMaxLines(2);
                detail.setEllipsize(TextUtils.TruncateAt.END);
                card.addView(detail);
            }
            for (GateItem gate : waitingGates) {
                LinearLayout card = new LinearLayout(this);
                card.setOrientation(LinearLayout.VERTICAL);
                card.setPadding(dp(12), dp(11), dp(12), dp(11));
                card.setBackgroundResource(R.drawable.card_gray);
                LinearLayout.LayoutParams params = matchWrap();
                params.bottomMargin = dp(8);
                section.addView(card, params);
                card.addView(label(gate.context + " · waiting", 14, COLOR_TEXT, true));
                card.addView(spacer(6));
                TextView detail = label(gate.summary, 13, COLOR_TEXT, false);
                detail.setMaxLines(2);
                detail.setEllipsize(TextUtils.TruncateAt.END);
                card.addView(detail);
            }
            for (QueueEvent event : queue) {
                LinearLayout card = new LinearLayout(this);
                card.setOrientation(LinearLayout.VERTICAL);
                card.setPadding(dp(12), dp(11), dp(12), dp(11));
                card.setBackgroundResource(R.drawable.card_gray);
                LinearLayout.LayoutParams params = matchWrap();
                params.bottomMargin = dp(8);
                section.addView(card, params);
                String state = queueStateLabel(event.state);
                card.addView(label(event.kind + (state.isEmpty() ? "" : " · " + state), 14, queueStateColor(event.state), true));
                card.addView(spacer(5));
                card.addView(label(firstNonEmpty(event.author, "Gitea") + " · " + firstNonEmpty(event.updatedAt, event.createdAt), 11, COLOR_MUTED, true));
                card.addView(spacer(6));
                TextView detail = label(event.summary, 13, COLOR_TEXT, false);
                detail.setMaxLines(4);
                detail.setEllipsize(TextUtils.TruncateAt.END);
                card.addView(detail);
            }
        }
        contentList.addView(section, sectionParams());
    }

    private void addBody(String body) {
        LinearLayout section = section("PR Body");
        TextView text = label(body.trim().isEmpty() ? "PR body 为空" : body, 13, COLOR_TEXT, false);
        section.addView(text, matchWrap());
        contentList.addView(section, sectionParams());
    }

    private LinearLayout section(String title) {
        LinearLayout section = new LinearLayout(this);
        section.setOrientation(LinearLayout.VERTICAL);
        section.setPadding(dp(12), dp(12), dp(12), dp(12));
        section.setBackgroundResource(R.drawable.card_white);
        section.addView(label(title, 10, "#5D6675", true));
        section.addView(spacer(9));
        return section;
    }

    private void postCiCommand(String command) {
        int number = applyPrInput();
        postingCommand = true;
        setStatus("正在评论 " + command + " 到 PR #" + number + "…", COLOR_MUTED);
        executor.execute(() -> {
            try {
                requestWithAuth("/repos/" + OWNER + "/" + REPO + "/issues/" + number + "/comments",
                        "POST", new JSONObject().put("body", command).toString(), headers("Content-Type", "application/json"));
                main.post(() -> {
                    Toast.makeText(this, "已发送 " + command, Toast.LENGTH_SHORT).show();
                    setStatus("已评论 " + command + "，正在刷新队列…", COLOR_MUTED);
                    main.postDelayed(() -> refreshSummary(true), 1500);
                });
            } catch (AuthRequired error) {
                main.post(() -> {
                    monitoring = false;
                    clearAccessToken();
                    showOAuth("需要 write:issue 权限，请重新授权后再发送 CI 命令。");
                });
            } catch (Exception error) {
                main.post(() -> setStatus("发送 " + command + " 失败：" + error.getMessage(), COLOR_DANGER));
            } finally {
                main.post(() -> postingCommand = false);
            }
        });
    }

    private JSONObject requestObject(String path) throws Exception {
        String text = requestWithAuth(path, "GET", null, headers("Accept", "application/json"));
        Object parsed = new JSONTokener(text).nextValue();
        if (parsed instanceof JSONObject) {
            return (JSONObject) parsed;
        }
        throw new IOException("expected object payload for " + path);
    }

    private List<JSONObject> requestArrayPages(String path, int maxPages) throws Exception {
        List<JSONObject> items = new ArrayList<>();
        for (int page = 1; page <= maxPages; page++) {
            String separator = path.contains("?") ? "&" : "?";
            String text = requestWithAuth(path + separator + "limit=100&page=" + page, "GET", null, headers("Accept", "application/json"));
            Object parsed = new JSONTokener(text).nextValue();
            if (!(parsed instanceof JSONArray)) {
                throw new IOException("expected list payload for " + path);
            }
            List<JSONObject> pageItems = jsonArrayToList((JSONArray) parsed);
            items.addAll(pageItems);
            if (pageItems.size() < 100) {
                break;
            }
        }
        return items;
    }

    private String requestWithAuth(String path, String method, String body, Map<String, String> extraHeaders) throws Exception {
        if (accessToken.trim().isEmpty()) {
            throw new AuthRequired("Gitea authorization is required");
        }
        try {
            return requestWithAuthScheme(path, method, body, extraHeaders, "bearer");
        } catch (AuthRequired error) {
            return requestWithAuthScheme(path, method, body, extraHeaders, "token");
        }
    }

    private String requestWithAuthScheme(String path, String method, String body, Map<String, String> extraHeaders, String scheme) throws Exception {
        Map<String, String> requestHeaders = new HashMap<>(extraHeaders);
        requestHeaders.put("Authorization", scheme + " " + accessToken);
        return requestRaw(API_BASE + path, method, body, requestHeaders);
    }

    private void validateAccessToken() throws Exception {
        requestWithAuth("/user", "GET", null, headers("Accept", "application/json"));
    }

    private void finishGeneratedToken(String token) {
        if (token.trim().isEmpty()) {
            showSetup("没有识别到新 token，请确认 Gitea 页面已生成访问 token。");
            return;
        }
        int generation = authGeneration;
        accessToken = token.trim();
        setLoading(true, "正在验证 token…");
        executor.execute(() -> {
            try {
                validateAccessToken();
                if (generation != authGeneration) {
                    return;
                }
                prefs.edit()
                        .putString(KEY_ACCESS_TOKEN, accessToken)
                        .putString(KEY_CLIENT_ID, "")
                        .putString(KEY_CLIENT_SECRET, "")
                        .putBoolean(KEY_MONITOR_ENABLED, true)
                        .apply();
                clientId = "";
                clientSecret = "";
                monitoring = true;
                main.post(() -> {
                    Toast.makeText(this, "授权验证通过", Toast.LENGTH_SHORT).show();
                    showMonitor();
                    ensureMonitorServiceState();
                    loadDefaultPrAndRefresh();
                });
            } catch (Exception error) {
                accessToken = "";
                main.post(() -> showSetup("token 验证失败：" + error.getMessage()));
            } finally {
                main.post(() -> setLoading(false, ""));
            }
        });
    }

    private String requestRaw(String url, String method, String body, Map<String, String> headers) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(20000);
        connection.setRequestMethod(method);
        for (Map.Entry<String, String> entry : headers.entrySet()) {
            connection.setRequestProperty(entry.getKey(), entry.getValue());
        }
        if (body != null) {
            connection.setDoOutput(true);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            connection.setRequestProperty("Content-Length", String.valueOf(bytes.length));
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
        }
        int code = connection.getResponseCode();
        String text = readStream(code >= 400 ? connection.getErrorStream() : connection.getInputStream());
        connection.disconnect();
        if (code == 401 || code == 403) {
            throw new AuthRequired("Gitea API " + code + ": " + text);
        }
        if (code >= 400) {
            throw new IOException("Gitea API " + code + ": " + text);
        }
        return text;
    }

    private String readStream(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        StringBuilder builder = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                builder.append(line);
            }
        }
        return builder.toString();
    }

    private void setLoading(boolean value, String message) {
        loading = value;
        if (!message.isEmpty()) {
            setStatus(message, COLOR_WARNING);
        }
    }

    private int applyPrInput() {
        if (prInput != null) {
            try {
                int parsed = Integer.parseInt(prInput.getText().toString().trim());
                if (parsed > 0) {
                    prNumber = parsed;
                    return prNumber;
                }
            } catch (NumberFormatException ignored) {
            }
        }
        prNumber = DEFAULT_PR_NUMBER;
        if (prInput != null) {
            prInput.setText(String.valueOf(DEFAULT_PR_NUMBER));
        }
        return prNumber;
    }

    private void scheduleMonitorTimer() {
        clearMonitorTimer();
        monitorRunnable = () -> refreshSummary(false);
        main.postDelayed(monitorRunnable, REFRESH_INTERVAL_MS);
    }

    private void clearMonitorTimer() {
        if (monitorRunnable != null) {
            main.removeCallbacks(monitorRunnable);
            monitorRunnable = null;
        }
    }

    private void clearAccessToken() {
        accessToken = "";
        authGeneration++;
        prefs.edit().remove(KEY_ACCESS_TOKEN).commit();
    }

    private void persistMonitorState(boolean enabled) {
        prefs.edit()
                .putBoolean(KEY_MONITOR_ENABLED, enabled)
                .putInt(KEY_MONITOR_PR_NUMBER, prNumber)
                .apply();
    }

    private void startPrMonitorService() {
        persistMonitorState(true);
        Intent intent = new Intent(this, PrMonitorService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    private void ensureMonitorServiceState() {
        if (MonitorLifecyclePolicy.shouldRunService(monitoring, accessToken)) {
            startPrMonitorService();
        } else {
            stopService(new Intent(this, PrMonitorService.class));
        }
    }

    private void resetOAuthState() {
        authGeneration++;
        autoCreateOAuth = false;
        autoCreateSubmitted = false;
        autoCreateToken = false;
        autoCreateTokenSubmitted = false;
        accessToken = "";
        clientId = "";
        clientSecret = "";
        prefs.edit()
                .remove(KEY_ACCESS_TOKEN)
                .remove(KEY_CLIENT_ID)
                .remove(KEY_CLIENT_SECRET)
                .remove(KEY_MONITOR_ENABLED)
                .remove(KEY_MONITOR_PR_NUMBER)
                .commit();
    }

    private void resetOAuthSetup(String message) {
        resetOAuthState();
        showSetup(message.isEmpty() ? "已清空旧 OAuth 配置，请点“一键授权并验证”。" : message + "\n已清空旧 OAuth 配置，请点“一键授权并验证”。");
    }

    private void logoutFromApp() {
        monitoring = false;
        persistMonitorState(false);
        stopService(new Intent(this, PrMonitorService.class));
        clearMonitorTimer();
        resetOAuthState();
        Toast.makeText(this, "已退出本应用授权", Toast.LENGTH_SHORT).show();
        showSetup("已退出本应用授权；Gitea 登录态保留，请点“一键授权并验证”重新授权。");
    }

    private void setStatus(String text, String color) {
        if (statusView != null) {
            statusView.setText(text);
            statusView.setTextColor(Color.parseColor(color));
        }
    }

    private String normalizeGateContext(String context) {
        String lower = context.toLowerCase(Locale.ROOT);
        if (lower.contains("protected-file-approval")) return "protected-file-approval";
        if (lower.contains("taichu/codex-pr-test-review")) return "taichu/codex-pr-test-review";
        if (lower.contains("taichu/codex-pr-review")) return "taichu/codex-pr-review";
        if (lower.contains("taichu/pr-build")) return "taichu/pr-build";
        if (lower.contains("taichu/dev-cloud-preflight")) return "taichu/dev-cloud-preflight";
        if (lower.contains("ci/merge-gate")) return "ci/merge-gate";
        return "";
    }

    private List<JSONObject> jsonArrayToList(JSONArray array) throws JSONException {
        List<JSONObject> items = new ArrayList<>();
        for (int index = 0; index < array.length(); index++) {
            JSONObject item = array.optJSONObject(index);
            if (item != null) {
                items.add(item);
            }
        }
        return items;
    }

    private String randomToken(int length) {
        String alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
        StringBuilder builder = new StringBuilder();
        for (int index = 0; index < length; index++) {
            builder.append(alphabet.charAt(random.nextInt(alphabet.length())));
        }
        return builder.toString();
    }

    private String isoNow() {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.ROOT);
        format.setTimeZone(TimeZone.getDefault());
        return format.format(new Date());
    }

    private String compactTime(String value) {
        if (value.length() < 19) {
            return "刚刚刷新";
        }
        return "刷新于 " + value.substring(11, 19);
    }

    private String cleanCommentText(String value) {
        String cleaned = valueOrEmpty(value)
                .replaceAll("(?s)<!--.*?-->", "")
                .replaceAll("(?s)<[^>]*>", "")
                .replace("&nbsp;", " ")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&")
                .replaceAll("(?m)^\\s*#+\\s*", "")
                .replaceAll("\\n{3,}", "\n\n")
                .trim();
        return cleaned.isEmpty() ? "评论无可展示内容" : cleaned;
    }

    private GateItem gateFromComment(JSONObject comment, String currentHeadSha) {
        String body = comment.optString("body", "");
        String lower = body.toLowerCase(Locale.ROOT);
        if (isQueueStatusComment(body)) {
            return null;
        }
        String context = "";
        if (lower.contains("taichu-dev-cloud-preflight") || lower.contains("taichu/dev-cloud-preflight")) {
            context = "taichu/dev-cloud-preflight";
        } else if ((lower.contains("external-ci/jenkins-merge-gate-test")
                || lower.contains("taichu merge gate：执行结果")
                || lower.contains("taichu merge gate: 执行结果")
                || lower.contains("taichu-ci/auto-merge-blocked"))
                && !lower.contains("merge-gate-onboard")
                && !lower.contains("merge-gate-queue-status")
                && !lower.contains("merge-gate-build-timing")) {
            context = "ci/merge-gate";
        }
        if (context.isEmpty()) {
            return null;
        }
        if (referencesDifferentHead(body, currentHeadSha)) {
            return null;
        }

        GateItem item = new GateItem();
        item.context = context;
        item.updatedAt = firstNonEmpty(comment.optString("updated_at", ""), comment.optString("created_at", ""));
        item.targetUrl = "";
        item.summary = cleanCommentText(body);
        item.state = stateFromComment(body);
        return item;
    }

    private GateItem missingGate(String context, String currentHeadSha, String summary) {
        GateItem item = new GateItem();
        item.context = context;
        item.state = "missing";
        item.summary = "当前 head " + shortSha(currentHeadSha) + "：" + summary;
        item.updatedAt = isoNow();
        return item;
    }

    private String stateFromComment(String value) {
        if (isBuildTimingComment(value)) {
            return "unknown";
        }
        String lower = valueOrEmpty(value).toLowerCase(Locale.ROOT);
        if (lower.contains("执行结果：成功")
                || lower.contains("执行结果: 成功")
                || lower.contains("build success")
                || lower.contains("merge gate success")
                || lower.contains("preflight: 通过")
                || lower.contains("preflight：通过")) {
            return "success";
        }
        if (lower.contains("暂不能入队")
                || lower.contains("执行结果：失败")
                || lower.contains("执行结果: 失败")
                || lower.contains("失败摘要")
                || lower.contains("未通过")
                || lower.contains("failed")
                || lower.contains("failure")) {
            return "failure";
        }
        if (!isInactiveQueueComment(value)
                && (lower.contains("queued") || lower.contains("running") || lower.contains("排队") || lower.contains("运行中"))) {
            return "pending";
        }
        if (lower.contains("通过") || lower.contains("success")) {
            return "success";
        }
        return "unknown";
    }

    private boolean isInactiveQueueComment(String value) {
        String lower = valueOrEmpty(value).toLowerCase(Locale.ROOT);
        return lower.contains("当前不在")
                || lower.contains("已离开活动队列")
                || lower.contains("not in")
                || lower.contains("not currently in")
                || lower.contains("no longer in");
    }

    private boolean isBuildTimingComment(String value) {
        String lower = valueOrEmpty(value).toLowerCase(Locale.ROOT);
        return lower.contains("build-timing")
                || value.contains("构建阶段耗时表")
                || value.contains("与主结果评论分开发帖")
                || lower.contains("testreport/build-timing");
    }

    private boolean isSuccessfulQueueEvent(QueueEvent event) {
        String state = valueOrEmpty(event.state).toLowerCase(Locale.ROOT);
        if (state.equals("success")) {
            return true;
        }
        String summary = valueOrEmpty(event.summary).toLowerCase(Locale.ROOT);
        return summary.contains("执行结果：成功")
                || summary.contains("执行结果: 成功")
                || summary.contains("success");
    }

    private String queueKind(String value) {
        String lower = valueOrEmpty(value).toLowerCase(Locale.ROOT);
        if (lower.contains("/ci merge") || lower.contains("merge gate") || lower.contains("merge-gate")) {
            return "merge gate";
        }
        if (lower.contains("/ci build") || lower.contains("pr build") || lower.contains("pr-build")) {
            return "PR build";
        }
        if (lower.contains("preflight")) {
            return "preflight";
        }
        return "队列状态";
    }

    private String queueSummary(String body, String currentHeadSha) {
        String cleaned = cleanCommentText(body);
        List<String> facts = new ArrayList<>();
        if (referencesDifferentHead(body, currentHeadSha)) {
            addFact(facts, "旧 head：当前 head " + shortSha(currentHeadSha) + " 尚未发现对应队列结果");
        }
        String command = extractCommand(cleaned);
        if (!command.isEmpty()) {
            addFact(facts, "命令：" + command);
        }

        for (String rawLine : cleaned.split("\\n")) {
            String line = compactQueueLine(rawLine);
            if (line.isEmpty() || !isUsefulQueueLine(line)) {
                continue;
            }
            addFact(facts, line);
            if (facts.size() >= 4) {
                break;
            }
        }
        if (facts.isEmpty()) {
            addFact(facts, truncateOneLine(cleaned, 180));
        }
        return joinLines(facts);
    }

    private String extractCommand(String text) {
        String lower = valueOrEmpty(text).toLowerCase(Locale.ROOT);
        if (lower.contains("/ci merge")) {
            return "/ci merge";
        }
        if (lower.contains("/ci build")) {
            return "/ci build";
        }
        return "";
    }

    private String exactCiCommand(String text) {
        return CiCommandQueueEvents.exactCommand(text);
    }

    private String compactQueueLine(String rawLine) {
        String line = valueOrEmpty(rawLine)
                .replaceAll("(?s)\\[([^\\]]+)]\\([^)]*\\)", "$1")
                .replace("`", "")
                .replace("**", "")
                .replace("__", "")
                .replaceAll("^\\s*>+\\s*", "")
                .trim();
        if (line.isEmpty()
                || line.matches("^[|\\-: ]+$")
                || line.contains("若此前本条评论")
                || line.contains("通常表示")
                || line.contains("以最终执行结果为准")) {
            return "";
        }
        if (line.startsWith("|") && line.endsWith("|")) {
            String[] cells = line.split("\\|");
            List<String> parts = new ArrayList<>();
            for (String cell : cells) {
                String part = cell.trim();
                if (!part.isEmpty() && !part.matches("^[-: ]+$")) {
                    parts.add(part);
                }
            }
            if (parts.size() >= 2) {
                line = parts.get(0) + "：" + parts.get(1);
                if (parts.size() >= 3 && line.length() < 90) {
                    line += " / " + parts.get(2);
                }
            }
        }
        line = line.replaceAll("\\s+", " ").trim();
        return truncateOneLine(line, 150);
    }

    private boolean isUsefulQueueLine(String value) {
        String lower = valueOrEmpty(value).toLowerCase(Locale.ROOT);
        return value.contains("暂不能入队")
                || value.contains("不能入队")
                || value.contains("未通过")
                || value.contains("失败")
                || value.contains("错误")
                || value.contains("超时")
                || value.contains("执行结果")
                || value.contains("失败摘要")
                || value.contains("原因")
                || value.contains("队列")
                || value.contains("排队")
                || value.contains("入队")
                || value.contains("位置")
                || lower.contains("blocked")
                || lower.contains("failure")
                || lower.contains("failed")
                || lower.contains("error")
                || lower.contains("timeout")
                || lower.contains("stale")
                || lower.contains("queued")
                || lower.contains("running")
                || lower.contains("pending")
                || lower.contains("status")
                || lower.contains("result")
                || lower.contains("head")
                || lower.contains("sha")
                || lower.contains("commit")
                || lower.contains("build")
                || lower.contains("preflight")
                || lower.contains("merge gate")
                || lower.contains("jenkins")
                || lower.contains("artifact");
    }

    private String queueStateLabel(String state) {
        String value = valueOrEmpty(state).toLowerCase(Locale.ROOT);
        if (value.equals("failure")) {
            return "失败/阻塞";
        }
        if (value.equals("pending")) {
            return "排队/运行中";
        }
        if (value.equals("success")) {
            return "成功";
        }
        return "";
    }

    private String queueStateColor(String state) {
        String value = valueOrEmpty(state).toLowerCase(Locale.ROOT);
        if (value.equals("failure")) {
            return COLOR_DANGER;
        }
        if (value.equals("success")) {
            return COLOR_SUCCESS;
        }
        return COLOR_TEXT;
    }

    private void addFact(List<String> facts, String value) {
        String fact = valueOrEmpty(value).trim();
        if (fact.isEmpty()) {
            return;
        }
        for (String existing : facts) {
            if (existing.equals(fact)) {
                return;
            }
        }
        facts.add(fact);
    }

    private String joinLines(List<String> lines) {
        StringBuilder builder = new StringBuilder();
        for (String line : lines) {
            if (builder.length() > 0) {
                builder.append('\n');
            }
            builder.append(line);
        }
        return builder.toString();
    }

    private String truncateOneLine(String value, int maxChars) {
        String text = valueOrEmpty(value).replaceAll("\\s+", " ").trim();
        if (text.length() <= maxChars) {
            return text;
        }
        return text.substring(0, Math.max(0, maxChars - 1)).trim() + "…";
    }

    private boolean referencesDifferentHead(String body, String currentHeadSha) {
        return GateHeadMatcher.referencesDifferentHead(body, currentHeadSha);
    }

    private String shortSha(String value) {
        if (value == null || value.length() < 7) {
            return "unknown";
        }
        return value.substring(0, 7);
    }

    private boolean isSuccessfulGate(GateItem item) {
        return GateStateClassifier.isSuccessful(item.state, item.summary);
    }

    private String formatState(String state) {
        String value = valueOrEmpty(state).trim();
        return value.isEmpty() ? "" : " · " + value;
    }

    private String firstNonEmpty(String first, String second) {
        return first == null || first.isEmpty() ? valueOrEmpty(second) : first;
    }

    private String valueOrEmpty(String value) {
        return value == null ? "" : value;
    }

    private String form(String key, String value) {
        return Uri.encode(key) + "=" + Uri.encode(value);
    }

    private Map<String, String> headers(String... pairs) {
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index + 1 < pairs.length; index += 2) {
            result.put(pairs[index], pairs[index + 1]);
        }
        return result;
    }

    private String jsString(String value) {
        return JSONObject.quote(value);
    }

    private String unquoteJsResult(String result) {
        try {
            Object parsed = new JSONTokener(result).nextValue();
            return parsed instanceof String ? (String) parsed : result;
        } catch (Exception ignored) {
            return result == null ? "" : result;
        }
    }

    private TextView label(String text, int sp, String color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(sp);
        view.setTextColor(Color.parseColor(color));
        view.setLineSpacing(dp(2), 1.0f);
        view.setIncludeFontPadding(false);
        if (bold) {
            view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        }
        return view;
    }

    private TextView titleLabel(String text) {
        TextView view = label(text, 18, COLOR_INK, true);
        view.setMaxLines(2);
        view.setEllipsize(TextUtils.TruncateAt.END);
        view.setLineSpacing(dp(1), 1.0f);
        return view;
    }

    private Button primaryButton(String text) {
        Button button = baseButton(text);
        button.setTextColor(Color.WHITE);
        button.setBackgroundResource(R.drawable.button_primary);
        return button;
    }

    private Button darkButton(String text) {
        Button button = baseButton(text);
        button.setTextColor(Color.WHITE);
        button.setBackgroundResource(R.drawable.button_dark);
        return button;
    }

    private Button outlineButton(String text) {
        Button button = baseButton(text);
        button.setTextColor(Color.parseColor(COLOR_PRIMARY));
        button.setBackgroundResource(R.drawable.button_outline);
        return button;
    }

    private Button baseButton(String text) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextSize(13);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setGravity(Gravity.CENTER);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setIncludeFontPadding(false);
        button.setPadding(dp(10), 0, dp(10), 0);
        button.setOnTouchListener((view, event) -> {
            int action = event.getActionMasked();
            if (action == MotionEvent.ACTION_DOWN) {
                view.animate().scaleX(0.98f).scaleY(0.98f).setDuration(80).start();
            } else if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                view.animate().scaleX(1f).scaleY(1f).setDuration(120).start();
            }
            return false;
        });
        return button;
    }

    private View spacer(int dp) {
        View view = new View(this);
        view.setLayoutParams(new LinearLayout.LayoutParams(1, dp(dp)));
        return view;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private LinearLayout.LayoutParams matchFixed(int heightDp) {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(heightDp));
    }

    private LinearLayout.LayoutParams fixed(int widthDp, int heightDp) {
        return new LinearLayout.LayoutParams(dp(widthDp), dp(heightDp));
    }

    private LinearLayout.LayoutParams weightFixed(int weight, int heightDp) {
        return new LinearLayout.LayoutParams(0, dp(heightDp), weight);
    }

    private LinearLayout.LayoutParams sectionParams() {
        LinearLayout.LayoutParams params = matchWrap();
        params.bottomMargin = dp(10);
        return params;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private int statusBarHeight() {
        int id = getResources().getIdentifier("status_bar_height", "dimen", "android");
        return id > 0 ? getResources().getDimensionPixelSize(id) : 0;
    }

    private static class AuthRequired extends IOException {
        AuthRequired(String message) {
            super(message);
        }
    }

    private static class PrSummary {
        int number;
        String title = "";
        String body = "";
        String state = "";
        String author = "";
        String headSha = "";
        String headRef = "";
        String baseRef = "";
        String fetchedAt = "";
        String latestCiCommand = "";
        String latestCiCommandAt = "";
        String latestCiCommandKey = "";
        Map<String, String> latestCiCommandAtByKind = new HashMap<>();
        List<GateItem> gates = new ArrayList<>();
        List<GateItem> successGates = new ArrayList<>();
        List<GateItem> waitingGates = new ArrayList<>();
        List<QueueEvent> queue = new ArrayList<>();
    }

    private static class GateItem {
        String context = "";
        String state = "";
        String summary = "";
        String updatedAt = "";
        String targetUrl = "";
    }

    private static class QueueEvent {
        String author = "";
        String createdAt = "";
        String updatedAt = "";
        String kind = "";
        String state = "";
        String summary = "";
    }

}
