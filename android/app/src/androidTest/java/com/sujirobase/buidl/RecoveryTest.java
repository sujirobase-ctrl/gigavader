package com.sujirobase.buidl;

import android.app.AlertDialog;
import android.content.Intent;
import android.view.View;
import android.view.ViewGroup;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.File;
import java.lang.reflect.Field;
import java.util.function.BooleanSupplier;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class RecoveryTest {
    private void until(BooleanSupplier condition)throws Exception{long end=System.currentTimeMillis()+60000;while(!condition.getAsBoolean()&&System.currentTimeMillis()<end)Thread.sleep(100);assertTrue("Timed out",condition.getAsBoolean());}
    private View find(View v,String key){if(Ui.hasKey(v,key))return v;if(v instanceof ViewGroup)for(int i=0;i<((ViewGroup)v).getChildCount();i++){View r=find(((ViewGroup)v).getChildAt(i),key);if(r!=null)return r;}return null;}
    private File partial(File folder){File[] files=folder.listFiles();if(files!=null)for(File f:files){if(f.isDirectory()){File found=partial(f);if(found!=null)return found;}else if(f.getName().endsWith(".part")&&f.length()>65536)return f;}return null;}
    @Test public void prepareInterruptedDownload()throws Exception{
        BuidlApplication app=ApplicationProvider.getApplicationContext();assertTrue(app.getPackageName().endsWith(".qa"));AppModel m=app.model;until(() -> m.engine!=null);
        InstrumentationRegistry.getInstrumentation().getUiAutomation().grantRuntimePermission(app.getPackageName(),android.Manifest.permission.POST_NOTIFICATIONS);
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)){
            m.engine.callAttr("scan","{\"url\":\"http://127.0.0.1:8876/threads/resume\",\"connections\":1,\"workers\":1}",true,false);
            until(() -> m.snapshot.optString("state").equals("ready")&&!m.snapshot.optBoolean("busy"));
            app.startForegroundService(new Intent(app,DownloadService.class).setAction("download").putExtra("queue",m.snapshot.optInt("queue_id")).putExtra("indices","[0]"));
            until(() -> partial(m.downloads)!=null);
            assertTrue(m.snapshot.optBoolean("busy"));
            System.out.println("BUIDL CRASH QA: partial persisted; terminate process now");
        }
    }
    @Test public void restoreAfterProcessDeath()throws Exception{
        BuidlApplication app=ApplicationProvider.getApplicationContext();AppModel m=app.model;until(() -> m.engine!=null && m.snapshot.has("recoveries"));
        File part=partial(m.downloads);assertNotNull("Part survives process death",part);long bytes=part.length();assertTrue(bytes<8*1024*1024);
        assertEquals(0,m.snapshot.optInt("scan_count"));
        JSONArray sessions=m.snapshot.getJSONArray("recoveries");int found=-1;for(int i=0;i<sessions.length();i++)if(sessions.getJSONObject(i).optString("title").equals("BUIDL Crash QA"))found=i;
        assertTrue(found>=0);final int selected=found;
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)){
            Thread.sleep(1000);
            scenario.onActivity(a -> {try{Field f=MainActivity.class.getDeclaredField("activeRecoveryDialog");f.setAccessible(true);AlertDialog d=(AlertDialog)f.get(a);assertNotNull("Startup recovery prompt",d);d.getListView().performItemClick(d.getListView().getChildAt(selected),selected,selected);}catch(Exception e){throw new RuntimeException(e);}});
            until(() -> m.snapshot.optString("state").equals("ready")&&m.items.length()==1);
            Thread.sleep(700);assertTrue(m.selected.contains(0));
            scenario.onActivity(a -> {View download=find(a.getWindow().getDecorView(),"baixar_selecionados");assertNotNull(download);assertTrue(download.isEnabled());download.performClick();});
            until(() -> m.snapshot.optString("state").equals("done")&&!m.snapshot.optBoolean("busy"));
            assertEquals(0,m.snapshot.optInt("scan_count"));assertEquals(1,m.snapshot.optInt("completed"));
            JSONArray files=new JSONArray(m.engine.callAttr("files").toString());File zip=null;
            for(int i=0;i<files.length();i++)if(files.getJSONObject(i).optString("name").equals("resume.zip"))zip=new File(files.getJSONObject(i).getString("path"));
            assertNotNull(zip);assertTrue(zip.isFile());
            try(java.util.zip.ZipFile archive=new java.util.zip.ZipFile(zip)){
                java.util.zip.ZipEntry entry=archive.getEntry("resume.bin");assertEquals(8*1024*1024,entry.getSize());
                java.util.zip.CRC32 crc=new java.util.zip.CRC32();try(java.io.InputStream in=archive.getInputStream(entry)){byte[] buffer=new byte[32768];int n;while((n=in.read(buffer))!=-1)crc.update(buffer,0,n);}assertEquals(entry.getCrc(),crc.getValue());
            }
            System.out.println("BUIDL CRASH QA: restored UI selection, resumed "+bytes+" bytes, complete ZIP, no rescan PASS");
        }
    }
}
