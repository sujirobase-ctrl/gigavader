package com.sujirobase.buidl;

import android.app.Activity;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

/** First-party login only. No JavaScript bridge, password extraction or cookie logging. */
public class CookieCaptureActivity extends Activity {
    static final String START_URL = "https://simpcity.cr/";
    protected WebView browser;
    private AppModel model;
    private TextView address, hint;
    private Button done;
    private ProgressBar progress;
    private Ui ui;
    private boolean saving, pageReady, destroyed, loadFailed;

    static String trustedOrigin(String url) {
        if (url == null) return null;
        try {
            Uri uri = Uri.parse(url);
            String host = uri.getHost();
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getUserInfo() != null
                    || (uri.getPort() != -1 && uri.getPort() != 443)) return null;
            if (!"simpcity.cr".equalsIgnoreCase(host) && !"www.simpcity.cr".equalsIgnoreCase(host)) return null;
            return "https://" + host.toLowerCase(java.util.Locale.ROOT) + "/";
        } catch (Exception ignored) { return null; }
    }

    @Override public void onCreate(Bundle state) {
        Ui.applyTheme(this);
        super.onCreate(state);
        ui=new Ui(this);ui.window(this);
        // Login contents must not appear in screenshots or the recent-apps preview.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        model = ((BuidlApplication)getApplication()).model;
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(ui.bg);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom()); return insets;
        });
        LinearLayout toolbar = new LinearLayout(this); toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(8),dp(4),dp(8),dp(4));
        Button back = ui.button("Voltar",false,this::leave);toolbar.addView(back,new LinearLayout.LayoutParams(-2,-2));
        address = ui.text("simpcity.cr",14,ui.text);address.setGravity(Gravity.CENTER);Ui.key(address,"origem_cookies");
        address.setPadding(dp(8),0,dp(8),0);
        toolbar.addView(address,new LinearLayout.LayoutParams(0,-2,1));
        done = ui.button("Pronto",true,this::capture); Ui.key(done,"salvar_cookies");done.setContentDescription("Pronto. Salvar cookies e voltar ao BUIDL");
        done.setEnabled(false); toolbar.addView(done,new LinearLayout.LayoutParams(-2,-2)); root.addView(toolbar);
        hint = ui.text("Entre no site e toque em Pronto para salvar sua sessão.",14,ui.muted);
        hint.setPadding(dp(12),dp(4),dp(12),dp(8));Ui.key(hint,"instrucao_cookies");hint.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);root.addView(hint);
        progress = new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);
        progress.setProgressTintList(ColorStateList.valueOf(ui.accent));progress.setContentDescription("Carregando o site");
        root.addView(progress,new LinearLayout.LayoutParams(-1,dp(3)));
        try { browser = new WebView(this); }
        catch (Exception unavailable) {
            setContentView(root); hint.setText("Android System WebView indisponível. Atualize-o ou use Importar cookies.txt."); return;
        }
        Ui.key(browser,"navegador_cookies");
        WebSettings settings = browser.getSettings();
        settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false); settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setJavaScriptCanOpenWindowsAutomatically(false); settings.setSupportMultipleWindows(false);
        settings.setSaveFormData(false);
        if (Build.VERSION.SDK_INT >= 26) {
            settings.setSafeBrowsingEnabled(true);
            browser.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        }
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(browser,false);
        browser.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view,int value) {
                progress.setProgress(value); progress.setVisibility(value==100 ? View.GONE : View.VISIBLE);
            }
        });
        browser.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request) {
                // CAPTCHA subframes retain normal WebView origin isolation.
                if (!request.isForMainFrame()) return false;
                return blockNavigation(request.getUrl().toString());
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view,String url) { return blockNavigation(url); }
            @Override public void onPageStarted(WebView view,String url,android.graphics.Bitmap icon) {
                pageReady=false; loadFailed=false; done.setEnabled(false);
                String origin=trustedOrigin(url);
                if(origin==null) { view.stopLoading(); hint.setText("A captura permite apenas HTTPS em simpcity.cr. Volte ao site para continuar."); }
                else address.setText(Uri.parse(origin).getHost());
            }
            @Override public void onPageFinished(WebView view,String url) {
                pageReady=!loadFailed && trustedOrigin(url)!=null && trustedOrigin(view.getUrl())!=null;
                done.setEnabled(pageReady && !saving);
            }
            @Override public void onReceivedSslError(WebView view,SslErrorHandler handler,SslError error) {
                handler.cancel(); loadFailed=true; pageReady=false; done.setEnabled(false);
                hint.setText("Certificado inválido. A conexão foi bloqueada para proteger seu login.");
            }
            @Override public void onReceivedError(WebView view,WebResourceRequest request,WebResourceError error) {
                if(request.isForMainFrame()) {
                    loadFailed=true; pageReady=false; done.setEnabled(false);
                    hint.setText("Não foi possível abrir o site. Volte e tente novamente, ou importe cookies.txt.");
                }
            }
        });
        browser.setDownloadListener((url,agent,disposition,mime,size) ->
                hint.setText("Esta tela serve apenas para login. Faça downloads pela aba Baixar."));
        root.addView(browser,new LinearLayout.LayoutParams(-1,0,1)); setContentView(root);
        openSite();
    }

    protected void openSite() { browser.loadUrl(START_URL); }

    private boolean blockNavigation(String url) {
        if(trustedOrigin(url)!=null) return false;
        hint.setText("Link externo bloqueado nesta tela. A captura é exclusiva do SimpCity."); return true;
    }

    private void capture() {
        if(saving || !pageReady) return;
        if(model.pending || model.engine==null || model.snapshot.optBoolean("busy")) {
            hint.setText("Aguarde a operação atual antes de salvar os cookies."); return;
        }
        String origin=trustedOrigin(browser.getUrl());
        if(origin==null) { hint.setText("Abra o SimpCity antes de capturar."); return; }
        // Native API includes HttpOnly cookies; document.cookie would miss them.
        // Read the origin root, not a login URL whose path-specific cookies could
        // accidentally be broadened to all threads.
        String cookies=CookieManager.getInstance().getCookie(origin);
        if(cookies==null || cookies.trim().isEmpty()) {
            hint.setText("Nenhum cookie disponível. Conclua o login e toque em Pronto novamente."); return;
        }
        String userAgent=browser.getSettings().getUserAgentString();
        String host=Uri.parse(origin).getHost();
        saving=true; model.pending=true; done.setEnabled(false); hint.setText("Salvando cookies com segurança…");
        browser.stopLoading();
        model.io.execute(() -> {
            boolean success;
            try {
                CookieManager.getInstance().flush();
                model.engine.callAttr("capture_cookies",host,cookies,userAgent); success=true;
            } catch(Exception ignored) { success=false; }
            finally { model.pending=false; }
            final boolean saved=success;
            model.main.post(() -> {
                saving=false;
                if(destroyed || isFinishing()) return;
                if(saved) { setResult(RESULT_OK); finish(); }
                else { done.setEnabled(pageReady); hint.setText("Não foi possível salvar. Os cookies anteriores foram mantidos. Tente novamente."); }
            });
        });
    }

    private void leave() {
        if(saving) { hint.setText("Aguarde o salvamento terminar."); return; }
        setResult(RESULT_CANCELED); finish();
    }
    @Override public void onBackPressed() { leave(); }
    @Override protected void onPause() { if(browser!=null)browser.onPause(); super.onPause(); }
    @Override protected void onResume() { super.onResume(); if(browser!=null)browser.onResume(); }
    @Override protected void onDestroy() {
        destroyed=true;
        if(browser!=null) { browser.stopLoading(); ((android.view.ViewGroup)browser.getParent()).removeView(browser); browser.destroy(); }
        super.onDestroy();
    }
    private int dp(int value) { return Math.round(value*getResources().getDisplayMetrics().density); }
}
