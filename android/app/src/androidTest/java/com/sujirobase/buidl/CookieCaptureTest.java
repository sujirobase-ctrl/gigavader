package com.sujirobase.buidl;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.widget.Button;
import android.widget.TextView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.chaquo.python.Python;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.File;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class CookieCaptureTest {
    private static void until(BooleanSupplier condition) throws Exception {
        long deadline=System.currentTimeMillis()+20000;
        while(!condition.getAsBoolean() && System.currentTimeMillis()<deadline)Thread.sleep(100);
        assertTrue("Timed out waiting for capture UI",condition.getAsBoolean());
    }
    private static View find(View v,String desc) {
        if(Ui.hasKey(v,desc))return v;
        if(desc.contentEquals(v.getContentDescription()==null ? "" : v.getContentDescription()))return v;
        if(v instanceof ViewGroup)for(int i=0;i<((ViewGroup)v).getChildCount();i++) {
            View found=find(((ViewGroup)v).getChildAt(i),desc);if(found!=null)return found;
        }
        return null;
    }
    private static void clearBrowser() throws Exception {
        CountDownLatch cleared=new CountDownLatch(1);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> CookieManager.getInstance().removeAllCookies(v -> cleared.countDown()));
        assertTrue(cleared.await(10,TimeUnit.SECONDS));
    }
    private static void seedCookie() throws Exception {
        CountDownLatch added=new CountDownLatch(1);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> CookieManager.getInstance().setCookie(
                CookieCaptureActivity.START_URL,"xf_session=buidl-qa-synthetic; Path=/; Secure; HttpOnly",ok -> {if(ok)added.countDown();}));
        assertTrue(added.await(10,TimeUnit.SECONDS));
    }
    private static Intent fixture(BuidlApplication app) {
        return new Intent().setClassName(app.getPackageName(),"com.sujirobase.buidl.QaCookieCaptureActivity");
    }

    @Test public void captureCookieWorkflow() throws Exception {
        BuidlApplication app=ApplicationProvider.getApplicationContext();
        assertEquals("Run this test only in the isolated QA variant","com.sujirobase.buidl.qa",app.getPackageName());
        AppModel model=app.model;until(() -> model.engine!=null);
        clearBrowser();
        // This is a different package, so the installed user's sessions and files
        // are never accessed or replaced by instrumentation.
        model.engine.callAttr("remove_cookies");
        try {
            Instrumentation instrumentation=InstrumentationRegistry.getInstrumentation();
            Instrumentation.ActivityMonitor monitor=instrumentation.addMonitor(CookieCaptureActivity.class.getName(),
                    new Instrumentation.ActivityResult(Activity.RESULT_CANCELED,null),true);
            try(ActivityScenario<MainActivity> main=ActivityScenario.launch(MainActivity.class)) {
                main.onActivity(a -> {View button=find(a.getWindow().getDecorView(),"capturar_cookies");assertNotNull(button);button.performClick();});
                assertEquals("Capture button launches the internal browser",1,monitor.getHits());
            } finally { instrumentation.removeMonitor(monitor); }

            try(ActivityScenario<CookieCaptureActivity> screen=ActivityScenario.launch(fixture(app))) {
                Thread.sleep(1000);
                screen.onActivity(a -> {
                    Button done=(Button)find(a.getWindow().getDecorView(),"salvar_cookies");assertTrue(done.isEnabled());
                    assertEquals("Pronto",done.getText().toString());done.performClick();
                    assertTrue(((TextView)find(a.getWindow().getDecorView(),"instrucao_cookies")).getText().toString().contains("Nenhum cookie"));
                    assertFalse(a.isFinishing());
                });
                assertFalse(new File(app.getFilesDir(),"cookies.txt").exists());
                seedCookie();
                screen.onActivity(a -> {
                    assertFalse(a.browser.getSettings().getAllowFileAccess());
                    assertFalse(a.browser.getSettings().getAllowContentAccess());
                    assertNull(CookieCaptureActivity.trustedOrigin("https://simpcity.cr.evil.invalid/"));
                    assertNull(CookieCaptureActivity.trustedOrigin("http://simpcity.cr/"));
                    assertNull(CookieCaptureActivity.trustedOrigin("https://simpcity.cr@evil.invalid/"));
                    assertNull(CookieCaptureActivity.trustedOrigin("https://simpcity.cr:8443/"));
                    assertNull(CookieCaptureActivity.trustedOrigin("file:///sdcard/cookies.txt"));
                    assertEquals("https://simpcity.cr/",CookieCaptureActivity.trustedOrigin("https://simpcity.cr/login/"));
                    find(a.getWindow().getDecorView(),"salvar_cookies").performClick();
                });
                until(() -> new File(app.getFilesDir(),"cookies.txt").exists() && !model.pending);
            }
            // Verify the actual embedded Python client, including persistence and
            // HttpOnly support, without printing values or making HTTP requests.
            var restarted=Python.getInstance().getModule("mobile_bridge").callAttr("Engine",
                    app.getFilesDir().getAbsolutePath(),model.downloads.getAbsolutePath());
            var client=restarted.callAttr("_client");
            try {
                assertTrue("HttpOnly cookie must survive native capture",
                        "buidl-qa-synthetic".equals(client.get("session").get("cookies").callAttr("get","xf_session").toString()));
                assertTrue(client.get("session").get("headers").callAttr("get","User-Agent").toString().contains("Android"));
            } finally { client.callAttr("close"); }

            long previous=new File(app.getFilesDir(),"cookies.txt").lastModified();
            clearBrowser();
            try(ActivityScenario<CookieCaptureActivity> screen=ActivityScenario.launch(fixture(app))) {
                Thread.sleep(1000);
                screen.onActivity(a -> {find(a.getWindow().getDecorView(),"salvar_cookies").performClick();a.onBackPressed();});
            }
            assertEquals("Empty capture/back must preserve previous saved session",previous,new File(app.getFilesDir(),"cookies.txt").lastModified());
            System.out.println("BUIDL QA: button, empty capture, HttpOnly, persisted Python session, safe origin and cancel PASS");
        } finally {
            clearBrowser(); model.engine.callAttr("remove_cookies");
            until(() -> !model.snapshot.optBoolean("cookies"));
        }
    }
}
