package kr.dronegcs.app;

import android.Manifest;
import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.MediaStore;
import android.provider.Settings;
import android.view.WindowManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.webkit.WebViewAssetLoader;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 드론 수색 GCS 안드로이드 앱.
 * web/ 폴더의 웹 앱을 https://appassets.androidplatform.net/assets/ 주소로 엽니다.
 * (file:// 로 열면 보안 연결로 인정되지 않아 위치 권한을 받을 수 없음)
 * 웹에서 할 수 없는 일은 window.GCSAndroid 로 제공합니다: 파일 저장, 화면 켜짐 유지, 진동, 앱 설정 열기.
 */
public class MainActivity extends Activity {
    private static final String HOST = "appassets.androidplatform.net";
    private static final String START_URL = "https://" + HOST + "/assets/index.html";
    private static final int REQ_LOCATION = 1;

    private WebView web;
    private WebViewAssetLoader assets;
    /** 권한 창이 떠 있는 동안 들어온 위치 요청들 (하나라도 놓치면 그 요청은 시간 초과까지 기다림) */
    private final List<Object[]> pendingGeo = new ArrayList<>();
    private long lastBack;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        assets = new WebViewAssetLoader.Builder()
                .setDomain(HOST)
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web = new WebView(this);
        web.setBackgroundColor(0xFF060D1D);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // localStorage (설정·임무 이력)
        s.setDatabaseEnabled(true);
        s.setGeolocationEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        // 앱은 https 주소지만, 같은 Wi-Fi 노트북의 브리지(ws://)에 연결할 수 있게 허용
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " DroneGCS-Android/1.0");
        s.setTextZoom(100);   // 기기 글자 크기 설정이 커도 관제 화면 배치가 깨지지 않게

        web.addJavascriptInterface(new NativeBridge(), "GCSAndroid");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                return assets.shouldInterceptRequest(req.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (HOST.equals(u.getHost())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;  // 외부 링크는 기본 브라우저로
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback cb) {
                if (hasLocationPermission()) {
                    cb.invoke(origin, true, false);
                    return;
                }
                pendingGeo.add(new Object[]{origin, cb});
                if (pendingGeo.size() == 1) {   // 권한 창은 한 번만 띄움
                    requestPermissions(new String[]{
                            Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
                }
            }
        });

        // 서비스워커(지도 타일 저장)도 앱 안 파일을 같은 https 주소로 받게 함
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient() {
                @Override
                public WebResourceResponse shouldInterceptRequest(WebResourceRequest req) {
                    return assets.shouldInterceptRequest(req.getUrl());
                }
            });
        }

        // 시스템이 앱을 정리했다가 다시 열 때 복원에 실패하면 처음부터 불러옴 (흰 화면 방지)
        if (saved == null || web.restoreState(saved) == null) web.loadUrl(START_URL);
    }

    private boolean hasLocationPermission() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code != REQ_LOCATION) return;
        boolean ok = hasLocationPermission();
        for (Object[] p : pendingGeo) ((GeolocationPermissions.Callback) p[1]).invoke((String) p[0], ok, false);
        pendingGeo.clear();
    }

    /** 뒤로 가기: 웹 앱이 열린 창(사진, 시트, 다른 메뉴)을 먼저 닫고, 지도 화면에서는 두 번 눌러 종료 */
    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        web.evaluateJavascript("window.gcsBack?window.gcsBack():false", handled -> {
            if ("true".equals(handled)) return;
            long now = System.currentTimeMillis();
            if (now - lastBack < 2000) {
                finish();
            } else {
                lastBack = now;
                Toast.makeText(this, "한 번 더 누르면 앱을 닫습니다", Toast.LENGTH_SHORT).show();
            }
        });
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }

    /** 웹 앱에서 window.GCSAndroid.xxx() 로 부르는 기능 */
    private class NativeBridge {
        /** 임무 중 화면 꺼짐 방지 */
        @JavascriptInterface
        public void keepScreenOn(boolean on) {
            runOnUiThread(() -> {
                if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }

        /** 진동. pattern 예: "180,80,180" (켜짐, 쉼, 켜짐 … 밀리초) */
        @JavascriptInterface
        public void vibrate(String pattern) {
            Vibrator v = (Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (v == null || !v.hasVibrator()) return;
            String[] parts = pattern.split(",");
            long[] timing = new long[parts.length + 1];   // 맨 앞 0 = 바로 시작
            for (int i = 0; i < parts.length; i++) {
                try { timing[i + 1] = Long.parseLong(parts[i].trim()); } catch (NumberFormatException ignored) { }
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) v.vibrate(VibrationEffect.createWaveform(timing, -1));
            else v.vibrate(timing, -1);
        }

        /** CSV·GeoJSON 저장. Android 10+ 는 '다운로드/DroneGCS' 폴더, 그 아래 버전은 앱 전용 다운로드 폴더. 저장 위치를 돌려줌, 실패하면 "" */
        @JavascriptInterface
        public String saveFile(String name, String mime, String text) {
            byte[] data = text.getBytes(StandardCharsets.UTF_8);
            String safe = name.replaceAll("[\\\\/:*?\"<>|]", "_");
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ContentValues cv = new ContentValues();
                    cv.put(MediaStore.MediaColumns.DISPLAY_NAME, safe);
                    cv.put(MediaStore.MediaColumns.MIME_TYPE, mime);
                    cv.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/DroneGCS");
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                    if (uri == null) return "";
                    try (OutputStream os = getContentResolver().openOutputStream(uri)) {
                        if (os == null) return "";
                        os.write(data);
                    }
                    return "다운로드/DroneGCS/" + safe;
                }
                File dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                if (dir == null) return "";
                File f = new File(dir, safe);
                try (FileOutputStream os = new FileOutputStream(f)) { os.write(data); }
                return f.getAbsolutePath();
            } catch (Exception e) {
                return "";
            }
        }

        /** 위치 권한을 '다시 묻지 않음'으로 거부했을 때 앱 설정 화면을 엶 */
        @JavascriptInterface
        public void openAppSettings() {
            runOnUiThread(() -> {
                Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", getPackageName(), null));
                startActivity(i);
            });
        }

        /** 기기 위치 서비스(GPS 스위치) 설정 화면을 엶 */
        @JavascriptInterface
        public void openLocationSettings() {
            runOnUiThread(() -> startActivity(new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)));
        }
    }
}
