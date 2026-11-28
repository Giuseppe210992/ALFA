package it.gpupool.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.content.res.AssetManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.SecureRandom;

/** Tiene vivo il server Python in primo piano (con notifica) finché l'app è installata e avviata. */
public class PoolService extends Service {
    static final int PORT = 8765;
    static volatile boolean started = false;
    static volatile String error = null;
    private PowerManager.WakeLock wakeLock;

    static String token(Context c) {
        SharedPreferences p = c.getSharedPreferences("pool", MODE_PRIVATE);
        String t = p.getString("token", null);
        if (t == null) {
            byte[] b = new byte[32];
            new SecureRandom().nextBytes(b);
            StringBuilder sb = new StringBuilder();
            for (byte x : b) sb.append(String.format("%02x", x));
            t = sb.toString();
            p.edit().putString("token", t).apply();
        }
        return t;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("pool", "GPU Pool", NotificationManager.IMPORTANCE_LOW));
        PendingIntent pi = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = new Notification.Builder(this, "pool")
                .setContentTitle("GPU Pool è attivo")
                .setContentText("Tocca per aprire")
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setContentIntent(pi)
                .build();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(1, n);
        }
        if (wakeLock == null) {          // con lo schermo spento la CPU non deve addormentarsi: il server deve restare raggiungibile
            wakeLock = ((PowerManager) getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "gpool:service");
        }
        if (!wakeLock.isHeld()) wakeLock.acquire(6 * 60 * 60 * 1000L);
        if (!started) {
            started = true;
            error = null;
            new Thread(this::boot, "gpool-python").start();
        }
        return START_STICKY;
    }

    private void boot() {
        try {
            File appDir = new File(getFilesDir(), "app");
            long ver = getPackageManager().getPackageInfo(getPackageName(), 0).getLongVersionCode();
            File marker = new File(getFilesDir(), "app.version");
            String want = String.valueOf(ver);
            String have = marker.exists() ? new String(java.nio.file.Files.readAllBytes(marker.toPath())).trim() : "";
            if (!want.equals(have) || !new File(appDir, "run_webapp.py").exists()) {
                copyAssets(getAssets(), "gpool", appDir);       // sovrascrive il codice; chiavi e risultati restano
                java.nio.file.Files.write(marker.toPath(), want.getBytes());
            }
            if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
            Python.getInstance().getModule("gpool_boot")
                    .callAttr("start", appDir.getAbsolutePath(), token(this), PORT);   // blocca finché il server gira
            error = "Il server si è fermato.";
        } catch (Throwable t) {
            error = t.toString();
        } finally {
            started = false;
        }
    }

    private static void copyAssets(AssetManager am, String path, File dst) throws Exception {
        String[] list = am.list(path);
        if (list == null || list.length == 0) {
            dst.getParentFile().mkdirs();
            try (InputStream in = am.open(path); OutputStream out = new FileOutputStream(dst)) {
                byte[] buf = new byte[65536];
                int r;
                while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
            }
        } else {
            dst.mkdirs();
            for (String f : list) copyAssets(am, path + "/" + f, new File(dst, f));
        }
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
