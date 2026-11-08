package com.sujirobase.buidl;

import android.content.Context;
import android.graphics.Bitmap;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.util.LruCache;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

public class AppModel {
    public volatile JSONObject snapshot = new JSONObject();
    public volatile JSONArray items = new JSONArray();
    public volatile PyObject engine;
    public volatile boolean pending;
    public final File downloads;
    public final Set<Integer> selected = new HashSet<>();
    public String query = "", kind = "Todos", host="Todos os hosts";
    public int page = 0;
    public int selectionQueue = 0;
    private volatile String lastError = "";
    private volatile long errorId = 0;
    public final ScheduledExecutorService io = Executors.newSingleThreadScheduledExecutor();
    public final java.util.concurrent.ExecutorService images = Executors.newFixedThreadPool(2);
    public final Handler main = new Handler(Looper.getMainLooper());
    public final LruCache<String, Bitmap> thumbnails = new LruCache<String, Bitmap>(8 * 1024 * 1024) {
        @Override protected int sizeOf(String key, Bitmap value) { return value.getAllocationByteCount(); }
    };
    public final Object videoLock=new Object();
    private Runnable selectionSave;
    public void persistSelection(){
        if(selectionSave!=null)main.removeCallbacks(selectionSave);
        final int queue=selectionQueue;final JSONArray ids=new JSONArray();for(int id:selected)ids.put(id);
        selectionSave=() -> io.execute(() -> {try{if(engine!=null)engine.callAttr("select",queue,ids.toString());}catch(Exception e){error(e);}});
        main.postDelayed(selectionSave,250);
    }
    private int loadedQueue = -1;

    public AppModel(Context context) {
        File external = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
        downloads = external != null ? external : new File(context.getFilesDir(), "downloads");
        io.execute(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
                Python.getInstance().getModule("android_gofile").callAttr("install", new SaltRuntime());
                engine = Python.getInstance().getModule("mobile_bridge").callAttr("Engine",
                        context.getFilesDir().getAbsolutePath(), downloads.getAbsolutePath(),new DestinationStorage(context));
                refresh();
            } catch (Exception e) { error(e); }
        });
        io.scheduleWithFixedDelay(this::refresh, 500, 500, TimeUnit.MILLISECONDS);
    }

    private void refresh() {
        if (engine == null) return;
        try {
            JSONObject next = new JSONObject(engine.callAttr("snapshot").toString());
            if (errorId != 0) { next.put("ui_error", errorId); next.put("error_message", lastError); }
            int queue = next.optInt("queue_id");
            if (queue != loadedQueue) {
                items = new JSONArray(engine.callAttr("items").toString());
                loadedQueue = queue;
            }
            snapshot = next;
        } catch (Exception e) { error(e); }
    }

    public void error(Exception error) {
        try {
            lastError = error.getMessage() == null ? "Não foi possível concluir a operação." : error.getMessage();
            errorId = System.currentTimeMillis();
            JSONObject next = new JSONObject(snapshot.toString());
            next.put("error_message", lastError);
            next.put("ui_error", errorId);
            if (engine == null) next.put("message", lastError);
            snapshot = next;
        } catch (Exception ignored) { }
    }

    public void call(String method, Consumer<PyObject> success, Object... args) {
        pending = true;
        io.execute(() -> {
            try {
                if (engine == null) throw new IllegalStateException("O mecanismo Python ainda não iniciou.");
                PyObject result = engine.callAttr(method, args);
                refresh();
                if (success != null) main.post(() -> success.accept(result));
            } catch (Exception e) { error(e); }
            finally { pending = false; }
        });
    }
}
