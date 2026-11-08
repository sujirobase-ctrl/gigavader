package com.sujirobase.buidl;

import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.TextView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONArray;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.File;
import java.io.FileOutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class SmokeTest {
    @Test public void cleanupFixtures() throws Exception {
        BuidlApplication context=ApplicationProvider.getApplicationContext();AppModel model=context.model;
        until(() -> model.engine!=null);assertFalse(model.snapshot.optBoolean("busy"));
        String title=InstrumentationRegistry.getArguments().getString("fixture_title","");
        assertTrue("Only a named synthetic fixture may be removed",title.matches("BUIDL Android QA [0-9]{6}"));
        File folder=new File(model.downloads,title);
        if(folder.exists()) {
            String base=folder.getCanonicalPath();assertTrue(base.startsWith(model.downloads.getCanonicalPath()+File.separator));
            java.util.Set<String> allowed=new java.util.HashSet<>(java.util.Arrays.asList("photo1.jpg","photo2.jpg","photo3.jpg","video.mp4","pack.zip",".fpd_state.json"));
            java.util.List<File> found=new java.util.ArrayList<>();collectFixture(folder,base,allowed,found);
            for(File file:found)assertTrue("Delete synthetic file: "+file,file.delete());
        }
        android.content.SharedPreferences prefs=context.getSharedPreferences("buidl",Context.MODE_PRIVATE);
        if(prefs.getString("url","").equals("http://127.0.0.1:8876/threads/qa"))prefs.edit().putString("url","").commit();
        assertFalse("Synthetic cookies must be removed through the UI",new File(context.getFilesDir(),"cookies.txt").exists());
        System.out.println("BUIDL QA: synthetic downloads removed; user folders untouched");
    }
    private static void collectFixture(File folder,String base,java.util.Set<String> allowed,java.util.List<File> found) throws Exception {
        File[] children=folder.listFiles();assertNotNull(children);
        for(File child:children){assertTrue(child.getCanonicalPath().startsWith(base+File.separator));
            if(child.isDirectory()){assertTrue(child.getName().equals("Photos")||child.getName().equals("Videos"));collectFixture(child,base,allowed,found);}
            else {assertTrue("Unexpected file: "+child,allowed.contains(child.getName()));assertTrue(child.length()<2*1024*1024);found.add(child);}
        }found.add(folder);
    }
    private static void until(BooleanSupplier condition) throws Exception {
        long end=System.currentTimeMillis()+45000;
        while(!condition.getAsBoolean()&&System.currentTimeMillis()<end)Thread.sleep(100);
        assertTrue("Timed out waiting for app state",condition.getAsBoolean());
    }
    private static View find(View root,String label) {
        if(Ui.hasKey(root,label))return root;
        if(label.contentEquals(root.getContentDescription()==null?"":root.getContentDescription()))return root;
        if(root instanceof TextView && label.contentEquals(((TextView)root).getText()))return root;
        if(root instanceof ViewGroup)for(int i=0;i<((ViewGroup)root).getChildCount();i++){View result=find(((ViewGroup)root).getChildAt(i),label);if(result!=null)return result;}
        return null;
    }
    private static void click(MainActivity activity,String label) {
        View view=find(activity.getWindow().getDecorView(),label);assertNotNull(label,view);assertTrue(label+" disabled",view.isEnabled());view.performClick();
    }
    private static AlertDialog videoDialog(MainActivity activity) throws Exception {
        Field field=MainActivity.class.getDeclaredField("activeVideoDialog");field.setAccessible(true);return (AlertDialog)field.get(activity);
    }
    private static void screenshot(Context context,String name) throws Exception {
        Bitmap bitmap=InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
        assertNotNull(bitmap);try(FileOutputStream out=new FileOutputStream(new File(context.getFilesDir(),name))){bitmap.compress(Bitmap.CompressFormat.PNG,100,out);}bitmap.recycle();
    }

    @Test public void fullWorkflow() throws Exception {
        BuidlApplication context=ApplicationProvider.getApplicationContext();AppModel model=context.model;
        if(android.os.Build.VERSION.SDK_INT>=33 && context.getPackageName().endsWith(".qa")) {
            InstrumentationRegistry.getInstrumentation().getUiAutomation().grantRuntimePermission(
                    context.getPackageName(),android.Manifest.permission.POST_NOTIFICATIONS);
        }
        until(() -> model.engine!=null);
        assertFalse("Do not use personal cookies in smoke tests",model.snapshot.optBoolean("cookies"));
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)) {
            scenario.onActivity(a -> { EditText url=(EditText)find(a.getWindow().getDecorView(),"url_thread");assertNotNull(url);url.setText("http://127.0.0.1:8876/threads/qa");click(a,"analisar_thread"); });
            until(() -> model.snapshot.optString("state").equals("ready")&&!model.snapshot.optBoolean("busy"));
            assertEquals(6,model.items.length());int scans=model.snapshot.optInt("scan_count");
            until(() -> model.thumbnails.get(model.snapshot.optInt("queue_id")+":2")!=null);
            assertNotNull("Video without a supplied thumbnail gets a real cover before playback",model.thumbnails.get(model.snapshot.optInt("queue_id")+":2"));
            scenario.onActivity(a -> {try{Method method=MainActivity.class.getDeclaredMethod("previewImage",int.class);method.setAccessible(true);method.invoke(a,0);}catch(Exception e){throw new RuntimeException(e);}});
            AtomicReference<Boolean> imageReady=new AtomicReference<>(false);
            until(() -> {scenario.onActivity(a -> {try{imageReady.set(find(videoDialog(a).getWindow().getDecorView(),"Imagem inteira, sem cortes")!=null);}catch(Exception e){throw new RuntimeException(e);}});return imageReady.get();});
            Thread.sleep(500);screenshot(context,"qa-image.png");scenario.onActivity(a -> {try{videoDialog(a).dismiss();}catch(Exception e){throw new RuntimeException(e);}});
            scenario.onActivity(a -> {click(a,"Novo");click(a,"Visualizar e escolher");CheckBox select=(CheckBox)find(a.getWindow().getDecorView(),"selecionar_1");assertNotNull(select);select.setChecked(true);click(a,"Novo");click(a,"Visualizar e escolher");assertTrue(model.selected.contains(1));});
            assertEquals(scans,model.snapshot.optInt("scan_count"));
            scenario.recreate();
            scenario.onActivity(a -> {assertTrue("selection survived recreation",model.selected.contains(1));click(a,"baixar_selecionados");});
            until(() -> model.snapshot.optString("state").equals("done")&&!model.snapshot.optBoolean("busy"));
            assertEquals(1,model.snapshot.optInt("completed"));assertEquals(0,model.snapshot.optInt("failed"));
            AtomicReference<JSONArray> files=new AtomicReference<>();model.call("files",r -> {try{files.set(new JSONArray(r.toString()));}catch(Exception e){throw new RuntimeException(e);}});
            until(() -> files.get()!=null);assertEquals(1,files.get().length());assertEquals("photo2.jpg",files.get().getJSONObject(0).getString("name"));
            scenario.onActivity(a -> {click(a,"Novo");click(a,"Visualizar e escolher");click(a,"baixar_selecionados");});
            until(() -> model.snapshot.optInt("skipped")==1&&!model.snapshot.optBoolean("busy"));
            assertEquals(scans,model.snapshot.optInt("scan_count"));
            System.out.println("BUIDL QA: two pages, selective download, reuse and resume PASS");
            screenshot(context,"qa-gallery.png");

            // Exercise the actual Android MediaPlayer, then a host serving HTML.
            for(int index:new int[]{2,3}) {
                scenario.onActivity(a -> {try{Method method=MainActivity.class.getDeclaredMethod("previewVideo",int.class);method.setAccessible(true);method.invoke(a,index);}catch(Exception e){throw new RuntimeException(e);}});
                AtomicReference<Boolean> ready=new AtomicReference<>(false);
                until(() -> {scenario.onActivity(a -> {try{AlertDialog d=videoDialog(a);View play=find(d.getWindow().getDecorView(),"Pausar / reproduzir");
                    ready.set(index==2 ? play!=null&&play.isEnabled() : find(d.getWindow().getDecorView(),"Prévia indisponível neste host. Mantivemos a capa; o arquivo ainda pode ser baixado.")!=null);
                }catch(Exception e){throw new RuntimeException(e);}});return ready.get();});
                if(index==2){AtomicReference<Boolean> rendered=new AtomicReference<>(false);until(() -> {scenario.onActivity(a -> {try{Field f=MainActivity.class.getDeclaredField("videoRendered");f.setAccessible(true);rendered.set(f.getBoolean(a));}catch(Exception e){throw new RuntimeException(e);}});return rendered.get();});}
                Thread.sleep(500);screenshot(context,index==2?"qa-video.png":"qa-video-fallback.png");
                if(index==2){ready.set(false);until(() -> {scenario.onActivity(a -> {try{ready.set(find(videoDialog(a).getWindow().getDecorView(),"Prévia de 10 segundos concluída")!=null);}catch(Exception e){throw new RuntimeException(e);}});return ready.get();});}
                scenario.onActivity(a -> {try{videoDialog(a).dismiss();}catch(Exception e){throw new RuntimeException(e);}});
                assertEquals(0,model.snapshot.optInt("failed"));
            }
            System.out.println("BUIDL QA: native video and cover fallback PASS");

            int queue=model.snapshot.optInt("queue_id");
            context.startForegroundService(new Intent(context,DownloadService.class).setAction("download").putExtra("queue",queue).putExtra("indices","null"));
            until(() -> model.snapshot.optString("state").equals("failed")&&!model.snapshot.optBoolean("busy"));
            assertEquals(4,model.snapshot.optInt("completed"));assertEquals(1,model.snapshot.optInt("skipped"));assertEquals(1,model.snapshot.optInt("failed"));
            assertTrue(model.snapshot.getJSONArray("errors").length()>0);
            screenshot(context,"qa-failures.png");
            files.set(null);model.call("files",r -> {try{files.set(new JSONArray(r.toString()));}catch(Exception e){throw new RuntimeException(e);}});
            until(() -> files.get()!=null);assertEquals(5,files.get().length());
            System.out.println("BUIDL QA: download all, honest failure count, saved files PASS");
            JSONArray rows=new org.json.JSONObject(model.engine.callAttr("queue",0,"failed").toString()).getJSONArray("rows");
            assertEquals(1,rows.length());assertEquals(3,rows.getJSONObject(0).getInt("index"));
            assertTrue(new JSONArray(model.engine.callAttr("history_json").toString()).length()>=3);
            String report=model.engine.callAttr("report").toString();assertFalse(report.contains("127.0.0.1"));assertFalse(report.contains("cookies.txt"));
            assertEquals("probe::en-US::TOKEN::1::abcdef12",new SaltRuntime().evaluate("function generateWT(t){return _sha256(navigator.userAgent+'::'+navigator.language+'::'+t+'::1::abcdef12');}"));
            try{new SaltRuntime().evaluate("while(true){}");fail("Unbounded JS should timeout");}catch(IllegalStateException expected){}
            System.out.println("BUIDL QA: covers, internal image, per-file queue, history, report and bounded JS PASS");
        }
    }
}
