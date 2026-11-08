package com.sujirobase.buidl;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.DocumentsContract;
import android.view.View;
import android.view.ViewGroup;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.TextView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.ByteArrayOutputStream;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class DestinationTest {
    private static void until(BooleanSupplier condition) throws Exception {
        long end=System.currentTimeMillis()+30000;
        while(!condition.getAsBoolean() && System.currentTimeMillis()<end)Thread.sleep(100);
        assertTrue("Timed out waiting for destination workflow",condition.getAsBoolean());
    }
    private static View find(View root,String label) {
        if(Ui.hasKey(root,label))return root;
        if(label.contentEquals(root.getContentDescription()==null ? "" : root.getContentDescription()))return root;
        if(root instanceof TextView && label.contentEquals(((TextView)root).getText()))return root;
        if(root instanceof ViewGroup)for(int i=0;i<((ViewGroup)root).getChildCount();i++) {
            View value=find(((ViewGroup)root).getChildAt(i),label);if(value!=null)return value;
        }return null;
    }
    private static void click(MainActivity activity,String label) {
        View button=find(activity.getWindow().getDecorView(),label);assertNotNull(label,button);assertTrue(button.isEnabled());button.performClick();
    }
    private static void openGallery(MainActivity activity) {
        try {java.lang.reflect.Method show=MainActivity.class.getDeclaredMethod("showTab",int.class);show.setAccessible(true);show.invoke(activity,1);}
        catch(Exception error){throw new RuntimeException(error);}
    }
    private static byte[] read(Context context,Uri uri) throws Exception {
        try(InputStream input=context.getContentResolver().openInputStream(uri);ByteArrayOutputStream output=new ByteArrayOutputStream()) {
            assertNotNull(input);byte[] buffer=new byte[8192];int count;
            while((count=input.read(buffer))!=-1)output.write(buffer,0,count);
            return output.toByteArray();
        }
    }
    private static JSONArray files(AppModel model) throws Exception { return new JSONArray(model.engine.callAttr("files").toString()); }
    private static int stagedImages(File root) {
        int count=0;File[] children=root.listFiles();if(children!=null)for(File f:children)count+=f.isDirectory() ? stagedImages(f) : (f.getName().endsWith(".jpg") ? 1 : 0);
        return count;
    }
    @Test public void selectedFolderWorkflow() throws Exception {
        BuidlApplication app=ApplicationProvider.getApplicationContext();AppModel model=app.model;
        assertEquals("Only isolated QA may use this fixture","com.sujirobase.buidl.qa",app.getPackageName());
        until(() -> model.engine!=null && !model.snapshot.optString("destination").isEmpty());
        assertFalse(model.snapshot.optBoolean("cookies"));
        String treeValue=model.snapshot.getString("destination");Uri tree=Uri.parse(treeValue);
        assertEquals("primary:Documents/BUIDL-DEST-QA-7f31a9",DocumentsContract.getTreeDocumentId(tree));
        assertEquals(0,files(model).length());
        if(android.os.Build.VERSION.SDK_INT>=33)InstrumentationRegistry.getInstrumentation().getUiAutomation().grantRuntimePermission(app.getPackageName(),android.Manifest.permission.POST_NOTIFICATIONS);
        Uri base=DocumentsContract.buildDocumentUriUsingTree(tree,DocumentsContract.getTreeDocumentId(tree));
        Uri collision;
        try(ActivityScenario<MainActivity> screen=ActivityScenario.launch(MainActivity.class)) {
            screen.onActivity(a -> {
                assertTrue(((TextView)find(a.getWindow().getDecorView(),"pasta_destino")).getText().toString().contains("BUIDL-DEST-QA"));
                ((EditText)find(a.getWindow().getDecorView(),"url_thread")).setText("http://127.0.0.1:8876/threads/qa");click(a,"analisar_thread");
            });
            until(() -> model.snapshot.optString("state").equals("ready") && !model.snapshot.optBoolean("busy"));
            assertEquals(6,model.items.length());int scans=model.snapshot.optInt("scan_count");
            // Create an unrelated same-name fixture before downloading: BUIDL
            // must preserve it and choose a new name for the downloaded image.
            Uri thread=DocumentsContract.createDocument(app.getContentResolver(),base,DocumentsContract.Document.MIME_TYPE_DIR,model.snapshot.getString("title"));
            assertNotNull(thread);
            Uri photos=DocumentsContract.createDocument(app.getContentResolver(),thread,DocumentsContract.Document.MIME_TYPE_DIR,"Photos");
            collision=DocumentsContract.createDocument(app.getContentResolver(),photos,"image/jpeg","photo2.jpg");
            assertNotNull(collision);
            try(OutputStream out=app.getContentResolver().openOutputStream(collision,"w")){assertNotNull(out);out.write("preexisting QA placeholder".getBytes(java.nio.charset.StandardCharsets.UTF_8));}
            screen.onActivity(a -> {openGallery(a);((CheckBox)find(a.getWindow().getDecorView(),"selecionar_1")).setChecked(true);});
            screen.recreate();
            screen.onActivity(a -> {assertTrue(model.selected.contains(1));click(a,"baixar_selecionados");});
            until(() -> !model.snapshot.optBoolean("busy") && model.snapshot.optString("state").equals("done"));
            assertEquals(1,model.snapshot.optInt("completed"));assertEquals(0,model.snapshot.optInt("failed"));
            JSONObject file=files(model).getJSONObject(0);assertTrue(file.getString("name").startsWith("photo2-"));
            byte[] image=read(app,Uri.parse(file.getString("uri")));assertEquals(0xff,image[0]&0xff);assertEquals(0xd8,image[1]&0xff);
            assertArrayEquals("preexisting QA placeholder".getBytes(java.nio.charset.StandardCharsets.UTF_8),read(app,collision));
            assertEquals(0,stagedImages(new File(app.getFilesDir(),"destinations")));
            screen.onActivity(a -> {openGallery(a);click(a,"baixar_selecionados");});
            until(() -> model.snapshot.optInt("skipped")==1 && !model.snapshot.optBoolean("busy"));
            assertEquals(scans,model.snapshot.optInt("scan_count"));assertEquals(1,files(model).length());
            screen.onActivity(a -> {click(a,"Downloads");click(a,"Abrir arquivos");});
            Thread.sleep(600);
            AtomicReference<Boolean> listed=new AtomicReference<>(false);
            screen.onActivity(a -> listed.set(find(a.getWindow().getDecorView(),"Abrir / visualizar")!=null));assertTrue(listed.get());

            model.call("download",null,model.snapshot.optInt("queue_id"),"null");
            until(() -> model.snapshot.optInt("failed")==1 && !model.snapshot.optBoolean("busy"));
            assertEquals(4,model.snapshot.optInt("completed"));assertEquals(1,model.snapshot.optInt("skipped"));
            assertEquals(5,files(model).length());
            for(int i=0;i<files(model).length();i++) {JSONObject item=files(model).getJSONObject(i);assertEquals(item.getLong("size"),read(app,Uri.parse(item.getString("uri"))).length);}
            assertArrayEquals("preexisting QA placeholder".getBytes(java.nio.charset.StandardCharsets.UTF_8),read(app,collision));

            PyObject restarted=Python.getInstance().getModule("mobile_bridge").callAttr("Engine",app.getFilesDir().getAbsolutePath(),model.downloads.getAbsolutePath(),new DestinationStorage(app));
            assertEquals(treeValue,new JSONObject(restarted.callAttr("snapshot").toString()).getString("destination"));
            assertEquals(5,new JSONArray(restarted.callAttr("files").toString()).length());
            // Only the QA grant is revoked. The normal app is untouched.
            app.getContentResolver().releasePersistableUriPermission(tree,android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION|android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            model.call("download",null,model.snapshot.optInt("queue_id"),"[1]");
            until(() -> model.snapshot.optString("state").equals("error") && !model.snapshot.optBoolean("busy"));
            assertEquals(0,model.snapshot.optInt("completed"));assertEquals(treeValue,model.snapshot.optString("destination"));
            assertTrue(model.snapshot.getJSONArray("errors").length()>0);
            System.out.println("BUIDL QA: SAF destination, collision preservation, selection, resume, auto delivery, restart and revoked grant PASS");
        }
    }
}
