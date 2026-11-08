package com.sujirobase.buidl;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.Intent;
import android.graphics.Bitmap;
import android.view.View;
import android.view.ViewGroup;
import android.widget.*;
import androidx.core.graphics.ColorUtils;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.File;
import java.io.FileOutputStream;
import static org.junit.Assert.*;

/** Isolated native-view checks, run at the emulator's current font size/orientation. */
@RunWith(AndroidJUnit4.class)
public class UiTest {
    private static View find(View view,String key) {
        if(Ui.hasKey(view,key) || view instanceof TextView && key.contentEquals(((TextView)view).getText()))return view;
        if(view instanceof ViewGroup)for(int i=0;i<((ViewGroup)view).getChildCount();i++) {View found=find(((ViewGroup)view).getChildAt(i),key);if(found!=null)return found;}
        return null;
    }
    private static boolean hasLabel(View view,int id) {
        if(view.getLabelFor()==id)return true;
        if(view instanceof ViewGroup)for(int i=0;i<((ViewGroup)view).getChildCount();i++)if(hasLabel(((ViewGroup)view).getChildAt(i),id))return true;
        return false;
    }
    private static void inspect(View view,float density) {
        if(view.getVisibility()!=View.VISIBLE)return;
        CharSequence description=view.getContentDescription();
        assertFalse("Technical ID leaked to screen reader",description!=null && description.toString().matches("(url_thread|selecionar_[0-9]+|.*_cookies|galeria_opcoes|pasta_destino)"));
        if((view instanceof Button || view instanceof EditText || view instanceof Spinner) && view.getWidth()>0) {
            assertTrue("Small touch height: "+view,view.getHeight()+1>=48*density);
            assertTrue("Small touch width: "+view,view.getWidth()+1>=48*density);
        }
        if(view instanceof TextView && !(view instanceof EditText)) {
            TextView t=(TextView)view;
            if(t.getLayout()!=null && t.getWidth()>0 && t.getText().length()>0) {
                for(int line=0;line<t.getLayout().getLineCount();line++)assertEquals("Truncated text: "+t.getText(),0,t.getLayout().getEllipsisCount(line));
                assertTrue("Clipped text height: "+t.getText(),t.getLayout().getHeight()<=t.getHeight()-t.getCompoundPaddingTop()-t.getCompoundPaddingBottom()+1);
            }
        }
        if(view instanceof ViewGroup)for(int i=0;i<((ViewGroup)view).getChildCount();i++)inspect(((ViewGroup)view).getChildAt(i),density);
    }
    private static void shot(Context context,String name) throws Exception {
        // Wait for the system activity-transition compositor, not only the UI queue.
        Thread.sleep(700);
        Bitmap bitmap=InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();assertNotNull(bitmap);
        try(FileOutputStream output=new FileOutputStream(new File(context.getFilesDir(),name))){bitmap.compress(Bitmap.CompressFormat.PNG,100,output);}bitmap.recycle();
    }
    @Test public void themesLabelsTargetsAndRecreation() throws Exception {
        BuidlApplication app=ApplicationProvider.getApplicationContext();assertEquals("com.sujirobase.buidl.qa",app.getPackageName());
        long deadline=System.currentTimeMillis()+30000;while(app.model.engine==null && System.currentTimeMillis()<deadline)Thread.sleep(100);assertNotNull(app.model.engine);
        SharedPreferences prefs=app.getSharedPreferences("buidl",Context.MODE_PRIVATE);
        int oldTheme=prefs.getInt("appearance",2);String oldUrl=prefs.getString("url","");boolean oldAdvanced=prefs.getBoolean("advanced_expanded",false);
        String suffix=InstrumentationRegistry.getArguments().getString("layout","default").replaceAll("[^a-z0-9-]","");
        try {
            if(suffix.startsWith("portrait") || suffix.startsWith("landscape"))
                assertTrue(InstrumentationRegistry.getInstrumentation().getUiAutomation().setRotation(suffix.startsWith("portrait") ? 0 : 1));
            if("true".equals(InstrumentationRegistry.getArguments().getString("populated"))) {
                assertFalse(app.model.snapshot.optBoolean("cookies"));
                InstrumentationRegistry.getInstrumentation().getUiAutomation().grantRuntimePermission(app.getPackageName(),android.Manifest.permission.POST_NOTIFICATIONS);
                app.startForegroundService(new Intent(app,DownloadService.class).setAction("scan").putExtra("settings","{\"url\":\"http://127.0.0.1:8876/threads/qa\",\"all_pages\":true}").putExtra("force",true).putExtra("all",false));
                deadline=System.currentTimeMillis()+30000;while(!app.model.snapshot.optString("state").equals("ready") && System.currentTimeMillis()<deadline)Thread.sleep(100);
                assertEquals(6,app.model.items.length());
            }
            for(int theme:new int[]{2,1}) {
                prefs.edit().putInt("appearance",theme).putString("url","").putBoolean("advanced_expanded",false).commit();
                Ui palette=new Ui(app);
                for(int bg:new int[]{palette.bg,palette.surface,palette.field})for(int fg:new int[]{palette.text,palette.muted,palette.accent,palette.success,palette.error})
                    assertTrue("Text contrast below 4.5:1",ColorUtils.calculateContrast(fg,bg)>=4.5);
                assertTrue(ColorUtils.calculateContrast(palette.onAccent,palette.accent)>=4.5);
                assertTrue(ColorUtils.calculateContrast(palette.line,palette.field)>=3);
                try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)) {
                    InstrumentationRegistry.getInstrumentation().waitForIdleSync();
                    scenario.onActivity(a -> {
                        android.content.res.Configuration config=a.getResources().getConfiguration();
                        if(suffix.startsWith("portrait"))assertEquals(android.content.res.Configuration.ORIENTATION_PORTRAIT,config.orientation);
                        if(suffix.startsWith("landscape"))assertEquals(android.content.res.Configuration.ORIENTATION_LANDSCAPE,config.orientation);
                        assertEquals("Actual system font scale",suffix.contains("large") ? 2f : 1f,config.fontScale,0.01f);
                        View root=a.getWindow().getDecorView();assertTrue(find(root,"Novo").isSelected());
                        View input=find(root,"url_thread");assertNotNull(input);assertTrue(hasLabel(root,input.getId()));
                        inspect(root,a.getResources().getDisplayMetrics().density);
                    });
                    shot(app,"ui-"+(theme==2?"dark":"light")+"-"+suffix+".png");
                    scenario.onActivity(a -> find(a.getWindow().getDecorView(),"opcoes_avancadas").performClick());
                    InstrumentationRegistry.getInstrumentation().waitForIdleSync();
                    scenario.onActivity(a -> {
                        View root=a.getWindow().getDecorView();
                        for(String key:new String[]{"incluir_arquivos","ignorar_arquivos","downloads_simultaneos"})assertTrue(hasLabel(root,find(root,key).getId()));
                        inspect(root,a.getResources().getDisplayMetrics().density);
                        ((EditText)find(root,"url_thread")).setText("https://example.invalid/threads/layout");
                        if(app.model.items.length()>0)find(root,"Visualizar e escolher").performClick();
                    });
                    InstrumentationRegistry.getInstrumentation().waitForIdleSync();
                    scenario.onActivity(a -> {View root=a.getWindow().getDecorView();if(app.model.items.length()>0)assertNotNull(find(root,"Pré-visualização e seleção"));inspect(root,a.getResources().getDisplayMetrics().density);});
                    shot(app,"ui-gallery-"+(theme==2?"dark":"light")+"-"+suffix+".png");
                    if(app.model.items.length()>0) {
                        scenario.onActivity(a -> {
                            View root=a.getWindow().getDecorView();View toggle=find(root,"Buscar e filtrar");if(toggle!=null)toggle.performClick();
                        });
                        InstrumentationRegistry.getInstrumentation().waitForIdleSync();
                        scenario.onActivity(a -> {View root=a.getWindow().getDecorView();for(String key:new String[]{"busca_galeria","tipo_arquivo"})assertTrue(hasLabel(root,find(root,key).getId()));inspect(root,a.getResources().getDisplayMetrics().density);});
                        scenario.onActivity(a -> {View toggle=find(a.getWindow().getDecorView(),"Ocultar busca e filtros");if(toggle!=null)toggle.performClick();});
                    }
                    scenario.recreate();
                    scenario.onActivity(a -> {View root=a.getWindow().getDecorView();find(root,"Novo").performClick();assertEquals("https://example.invalid/threads/layout",((EditText)find(root,"url_thread")).getText().toString());});
                }
            }
        } finally {prefs.edit().putInt("appearance",oldTheme).putString("url",oldUrl).putBoolean("advanced_expanded",oldAdvanced).commit();InstrumentationRegistry.getInstrumentation().getUiAutomation().setRotation(-2);}
        System.out.println("BUIDL UI: contrast, semantic labels, >=48dp targets, text layout and recreation PASS ("+suffix+")");
    }
}
