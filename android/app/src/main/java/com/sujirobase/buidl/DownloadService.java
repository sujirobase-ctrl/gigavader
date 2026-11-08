package com.sujirobase.buidl;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Handler;
import android.os.IBinder;
import android.os.PowerManager;
import org.json.JSONObject;

public class DownloadService extends Service {
    private static final String CHANNEL = "buidl_downloads";
    private final Handler handler = new Handler();
    private AppModel model;
    private long started;
    private PowerManager.WakeLock wakeLock;
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            JSONObject state = model.snapshot;
            if (!model.pending && !state.optBoolean("busy") && System.currentTimeMillis() - started > 2000) {
                stopSelf();
                return;
            }
            getSystemService(NotificationManager.class).notify(1, notification(state.optString("message", "Preparando…")));
            handler.postDelayed(this, 1500);
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        model = ((BuidlApplication)getApplication()).model;
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            getSystemService(NotificationManager.class).createNotificationChannel(
                    new NotificationChannel(CHANNEL, "Downloads do BUIDL", NotificationManager.IMPORTANCE_LOW));
        }
    }

    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, DownloadService.class).setAction("cancel");
        PendingIntent cancel = PendingIntent.getService(this, 1, stop, PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = android.os.Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        return builder.setSmallIcon(R.drawable.ic_buidl).setContentTitle("BUIDL")
                .setContentText(text).setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Parar fila", cancel).build()).build();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(1, notification("Preparando…"));
        started = System.currentTimeMillis();
        if (wakeLock == null) {
            wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "buidl:download");
            wakeLock.acquire(6 * 60 * 60 * 1000L);
        }
        if (intent != null) {
            String action = intent.getAction();
            if ("scan".equals(action)) {
                model.call("scan", null, intent.getStringExtra("settings"), intent.getBooleanExtra("force", false), intent.getBooleanExtra("all", false));
            } else if ("download".equals(action)) {
                model.call("download", null, intent.getIntExtra("queue", 0), intent.getStringExtra("indices"));
            } else if("recover".equals(action)) {
                model.call("recover",null,intent.getStringExtra("id"),false);
            } else if("retry".equals(action)) {
                model.call("retry",null,intent.getStringExtra("indices"));
            } else if("web".equals(action)) {
                // The browser's asynchronous Python bridge owns the operation.
            } else if ("cancel".equals(action)) model.call("cancel", null);
        }
        handler.removeCallbacks(tick);
        handler.postDelayed(tick, 1500);
        return START_NOT_STICKY;
    }

    @Override public void onTimeout(int startId, int fgsType) {
        model.call("cancel", null);
        stopSelf();
    }

    @Override public void onDestroy() {
        handler.removeCallbacks(tick);
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        stopForeground(true);
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
