package it.gpupool.app;

import android.Manifest;
import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.MediaStore;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.MimeTypeMap;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {
    private static final int REQ_FILE = 1, REQ_MEDIA = 2, REQ_NOTIF = 3;
    private WebView web;
    private ValueCallback<Uri[]> filePathCb;
    private PermissionRequest pendingPerm;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        web = new WebView(this);
        web.setLayoutParams(new ViewGroup.LayoutParams(-1, -1));
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, android.webkit.WebResourceRequest r) {
                Uri u = r.getUrl();
                if ("127.0.0.1".equals(u.getHost())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest r) {
                ui.post(() -> askMedia(r));
            }

            @Override
            public boolean onShowFileChooser(WebView w, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (filePathCb != null) filePathCb.onReceiveValue(null);
                filePathCb = cb;
                try {
                    startActivityForResult(p.createIntent(), REQ_FILE);
                } catch (Exception e) {
                    filePathCb = null;
                    return false;
                }
                return true;
            }
        });

        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }
        askBatteryExemption();
        web.loadData(page("Avvio di GPU Pool… (la prima volta ci vuole qualche secondo)"), "text/html; charset=utf-8", null);
        startForegroundService(new Intent(this, PoolService.class));
        waitForServer();
    }

    /** Chiede una sola volta di non limitare l'app in background: senza, Android può fermarla a schermo spento. */
    private void askBatteryExemption() {
        try {
            android.content.SharedPreferences p = getSharedPreferences("pool", MODE_PRIVATE);
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (p.getBoolean("battery_asked", false) || pm.isIgnoringBatteryOptimizations(getPackageName())) return;
            p.edit().putBoolean("battery_asked", true).apply();
            startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception ignored) { }
    }

    private static String page(String msg) {
        return "<meta name=viewport content='width=device-width'><body style='font-family:sans-serif;background:#0b0f19;color:#eef2ff;padding:2em'>"
                + "<h2>GPU Pool</h2><p>" + msg + "</p>";
    }

    private void waitForServer() {
        final String token = PoolService.token(this);
        new Thread(() -> {
            long end = System.currentTimeMillis() + 120000;
            while (System.currentTimeMillis() < end) {
                if (PoolService.error != null) {
                    final String e = PoolService.error.replace("<", "&lt;");
                    ui.post(() -> web.loadData(page("Errore di avvio:<br><code>" + e + "</code>"), "text/html; charset=utf-8", null));
                    return;
                }
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + PoolService.PORT + "/claim/status").openConnection();
                    c.setConnectTimeout(800);
                    c.setReadTimeout(1500);
                    if (c.getResponseCode() == 200) {
                        ui.post(() -> web.loadUrl("http://127.0.0.1:" + PoolService.PORT + "/#t=" + token));
                        return;
                    }
                } catch (Exception ignored) { }
                try { Thread.sleep(700); } catch (InterruptedException e) { return; }
            }
            ui.post(() -> web.loadData(page("Il programma non si avvia. Chiudi l'app e riaprila."), "text/html; charset=utf-8", null));
        }, "gpool-wait").start();
    }

    private void askMedia(PermissionRequest r) {
        pendingPerm = r;
        List<String> need = new ArrayList<>();
        for (String res : r.getResources()) {
            if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(res) && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.CAMERA);
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(res) && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.RECORD_AUDIO);
        }
        if (need.isEmpty()) {
            r.grant(r.getResources());
            pendingPerm = null;
        } else {
            requestPermissions(need.toArray(new String[0]), REQ_MEDIA);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == REQ_MEDIA && pendingPerm != null) {
            boolean ok = results.length > 0;
            for (int g : results) ok &= g == PackageManager.PERMISSION_GRANTED;
            if (ok) pendingPerm.grant(pendingPerm.getResources()); else pendingPerm.deny();
            pendingPerm = null;
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE && filePathCb != null) {
            filePathCb.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(res, data));
            filePathCb = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else moveTaskToBack(true);
    }

    private void toast(String m) { ui.post(() -> Toast.makeText(this, m, Toast.LENGTH_LONG).show()); }

    private void save(String name, String mime, InputStream in) throws Exception {
        name = name.substring(name.lastIndexOf('/') + 1);
        if (mime == null) {
            String ext = MimeTypeMap.getFileExtensionFromUrl(name);
            mime = ext == null ? null : MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext.toLowerCase());
            if (mime == null) mime = "application/octet-stream";
        }
        ContentValues v = new ContentValues();
        v.put(MediaStore.Downloads.DISPLAY_NAME, name);
        v.put(MediaStore.Downloads.MIME_TYPE, mime);
        v.put(MediaStore.Downloads.RELATIVE_PATH, "Download");
        Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        try (OutputStream out = getContentResolver().openOutputStream(uri)) {
            byte[] buf = new byte[65536];
            int r;
            while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
        } finally {
            in.close();
        }
        toast("Salvato in Download: " + name);
    }

    /** Ponte per la pagina: i download «blob» non funzionano nella WebView, quindi si salvano da qui. */
    class Bridge {
        @JavascriptInterface
        public void download(String path, String name, String token) {
            new Thread(() -> {
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + PoolService.PORT + path).openConnection();
                    c.setRequestProperty("X-Pool-Token", token);
                    c.setReadTimeout(0);
                    if (c.getResponseCode() != 200) throw new Exception("errore " + c.getResponseCode());
                    save(name, null, c.getInputStream());
                } catch (Exception e) {
                    toast("Download non riuscito: " + e.getMessage());
                }
            }, "gpool-save").start();
        }

        @JavascriptInterface
        public void saveText(String name, String text, String mime) {
            try {
                save(name, mime, new ByteArrayInputStream(text.getBytes("UTF-8")));
            } catch (Exception e) {
                toast("Salvataggio non riuscito: " + e.getMessage());
            }
        }
    }
}
