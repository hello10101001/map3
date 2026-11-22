package kr.dronegcs.app;

import android.Manifest;
import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.Context;
import android.content.pm.PackageManager;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbManager;
import android.app.PendingIntent;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
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
import android.webkit.ValueCallback;
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

import com.hoho.android.usbserial.driver.UsbSerialDriver;
import com.hoho.android.usbserial.driver.UsbSerialPort;
import com.hoho.android.usbserial.driver.UsbSerialProber;
import com.hoho.android.usbserial.util.SerialInputOutputManager;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
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
    private static final int REQ_FILE = 2;
    /** 승인 서류 고르기(input type=file) 결과를 웹에 돌려줄 곳 */
    private ValueCallback<Uri[]> fileCb;

    private WebView web;
    private WebViewAssetLoader assets;
    /** 권한 창이 떠 있는 동안 들어온 위치 요청들 (하나라도 놓치면 그 요청은 시간 초과까지 기다림) */
    private final List<Object[]> pendingGeo = new ArrayList<>();
    private long lastBack;

    /* ---------- 텔레메트리 무선기 (USB 시리얼) ---------- */
    private static final String USB_PERMISSION = "kr.dronegcs.app.USB_PERMISSION";
    private final Handler ui = new Handler(Looper.getMainLooper());
    private UsbSerialPort usbPort;
    private SerialInputOutputManager usbIo;
    private final ByteArrayOutputStream usbBuf = new ByteArrayOutputStream();
    private boolean usbFlushQueued;
    private int usbGen;   // 다시 연결할 때 이전 연결의 늦은 콜백을 버리려고

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
                if (!req.isForMainFrame()) return false;  // 앱 안의 작은 화면(열화상 칸)은 앱 안에서 열기
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;  // 외부 링크는 기본 브라우저로
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            /** 웹의 파일 고르기 칸(비행 승인 서류): 안드로이드 파일·사진 선택 창을 띄움 */
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                try {
                    startActivityForResult(p.createIntent(), REQ_FILE);
                } catch (Exception e) {
                    fileCb = null;
                    cb.onReceiveValue(null);
                    Toast.makeText(MainActivity.this, "파일 선택 창을 열 수 없습니다", Toast.LENGTH_SHORT).show();
                }
                return true;
            }

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

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        super.onActivityResult(code, result, data);
        if (code == REQ_FILE && fileCb != null) {
            fileCb.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            fileCb = null;
        }
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
        usbClose(false);
        web.destroy();
        super.onDestroy();
    }

    /** 웹에 USB 연결 상태를 알림: window.__usbState(상태, 안내) — 상태 open / error / closed */
    private void usbState(String st, String msg) {
        String js = "window.__usbState&&window.__usbState(" + JSONObject.quote(st) + "," + JSONObject.quote(msg) + ")";
        ui.post(() -> web.evaluateJavascript(js, null));
    }

    /** 무선기를 찾아 (권한을 받고) 엶. UI 스레드에서 부름 */
    private void usbOpen(int baud) {
        usbClose(false);
        final int gen = ++usbGen;
        UsbManager mgr = (UsbManager) getSystemService(Context.USB_SERVICE);
        List<UsbSerialDriver> drivers = mgr == null ? new ArrayList<>() : UsbSerialProber.getDefaultProber().findAllDrivers(mgr);
        if (drivers.isEmpty()) {
            usbState("error", "USB 무선기를 찾지 못했습니다. 텔레메트리 무선기를 태블릿 USB(OTG)에 꽂았는지 확인하세요.");
            return;
        }
        UsbSerialDriver drv = drivers.get(0);
        UsbDevice dev = drv.getDevice();
        if (!mgr.hasPermission(dev)) {
            int flags = Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
            PendingIntent pi = PendingIntent.getBroadcast(this, 0, new Intent(USB_PERMISSION).setPackage(getPackageName()), flags);
            mgr.requestPermission(dev, pi);
            usbState("wait", "USB 사용 허용 창에서 '확인'을 누르세요.");
            // 허용 결과는 1초마다 확인 (방송 수신기 등록 없이, 최대 60초)
            final long until = System.currentTimeMillis() + 60000;
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    if (gen != usbGen) return;
                    if (mgr.hasPermission(dev)) usbOpenWith(mgr, drv, baud, gen);
                    else if (System.currentTimeMillis() < until) ui.postDelayed(this, 1000);
                    else usbState("error", "USB 사용을 허용하지 않았습니다. '연결'을 다시 누르세요.");
                }
            }, 1000);
            return;
        }
        usbOpenWith(mgr, drv, baud, gen);
    }

    private void usbOpenWith(UsbManager mgr, UsbSerialDriver drv, int baud, int gen) {
        try {
            UsbDeviceConnection conn = mgr.openDevice(drv.getDevice());
            if (conn == null) throw new Exception("장치를 열 수 없습니다");
            UsbSerialPort port = drv.getPorts().get(0);
            port.open(conn);
            port.setParameters(baud, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE);
            try { port.setDTR(true); port.setRTS(true); } catch (Exception ignored) { }   // 일부 무선기는 DTR이 켜져야 데이터를 보냄
            usbPort = port;
            usbIo = new SerialInputOutputManager(port, new SerialInputOutputManager.Listener() {
                @Override public void onNewData(byte[] data) { usbData(data, gen); }
                @Override public void onRunError(Exception e) {
                    if (gen != usbGen) return;
                    usbClose(false);
                    usbState("error", "USB 연결이 끊겼습니다 (" + e.getMessage() + ")");
                }
            });
            usbIo.start();
            usbState("open", drv.getClass().getSimpleName().replace("SerialDriver", "") + " · " + baud + "bps");
        } catch (Exception e) {
            usbClose(false);
            usbState("error", "무선기를 열지 못했습니다: " + e.getMessage());
        }
    }

    /** 받은 바이트를 모아 50ms마다 한 번에 웹으로 (무선기는 작은 조각으로 자주 보내서 그대로 넘기면 화면이 느려짐) */
    private void usbData(byte[] data, int gen) {
        if (gen != usbGen) return;
        synchronized (usbBuf) {
            usbBuf.write(data, 0, data.length);
            if (usbFlushQueued) return;
            usbFlushQueued = true;
        }
        ui.postDelayed(() -> {
            byte[] chunk;
            synchronized (usbBuf) {
                chunk = usbBuf.toByteArray();
                usbBuf.reset();
                usbFlushQueued = false;
            }
            if (chunk.length == 0 || gen != usbGen) return;
            String b64 = Base64.encodeToString(chunk, Base64.NO_WRAP);
            web.evaluateJavascript("window.__usbData&&window.__usbData('" + b64 + "')", null);
        }, 50);
    }

    private void usbClose(boolean notify) {
        usbGen++;
        if (usbIo != null) { try { usbIo.stop(); } catch (Exception ignored) { } usbIo = null; }
        if (usbPort != null) { try { usbPort.close(); } catch (Exception ignored) { } usbPort = null; }
        synchronized (usbBuf) { usbBuf.reset(); usbFlushQueued = false; }
        if (notify) usbState("closed", "연결 끊음");
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

        /** 공식 비행금지구역 자료(브이월드) 받기: 웹에서 직접 부르면 CORS로 막혀서 앱이 대신 받아 window.__nativeHttp(id, 상태코드, 본문)로 돌려줌.
         *  안전을 위해 api.vworld.kr 주소만 허용 */
        @JavascriptInterface
        public void httpGet(String url, String id) {
            new Thread(() -> {
                int code = 0;
                String body;
                try {
                    URL u = new URL(url);
                    if (!"https".equals(u.getProtocol()) || !"api.vworld.kr".equals(u.getHost())) throw new Exception("허용되지 않은 주소");
                    HttpURLConnection c = (HttpURLConnection) u.openConnection();
                    c.setConnectTimeout(10000);
                    c.setReadTimeout(20000);
                    c.setRequestProperty("Referer", "https://" + HOST + "/");
                    code = c.getResponseCode();
                    InputStream is = code < 400 ? c.getInputStream() : c.getErrorStream();
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    if (is != null) {
                        byte[] buf = new byte[16384];
                        int n;
                        while ((n = is.read(buf)) > 0) bo.write(buf, 0, n);
                        is.close();
                    }
                    body = bo.toString("UTF-8");
                    c.disconnect();
                } catch (Exception e) {
                    code = 0;
                    body = String.valueOf(e.getMessage());
                }
                String js = "window.__nativeHttp&&window.__nativeHttp(" + JSONObject.quote(id) + "," + code + "," + JSONObject.quote(body) + ")";
                runOnUiThread(() -> web.evaluateJavascript(js, null));
            }).start();
        }

        /** 텔레메트리 무선기 열기 (baud: 보통 57600). 결과는 window.__usbState, 받은 데이터는 window.__usbData(base64) */
        @JavascriptInterface
        public void usbOpen(int baud) {
            runOnUiThread(() -> MainActivity.this.usbOpen(baud));
        }

        /** 무선기로 보내기 (MAVLink 바이트, base64). 성공하면 true */
        @JavascriptInterface
        public boolean usbWrite(String b64) {
            UsbSerialPort p = usbPort;
            if (p == null) return false;
            try {
                p.write(Base64.decode(b64, Base64.DEFAULT), 1000);
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public void usbClose() {
            runOnUiThread(() -> MainActivity.this.usbClose(true));
        }

        /** 탐지 위치 공유: 안드로이드 공유창(문자, 카카오톡, 메일 등)으로 글을 보냄 */
        @JavascriptInterface
        public void share(String text) {
            runOnUiThread(() -> {
                Intent send = new Intent(Intent.ACTION_SEND);
                send.setType("text/plain");
                send.putExtra(Intent.EXTRA_TEXT, text);
                startActivity(Intent.createChooser(send, "탐지 위치 공유"));
            });
        }
    }
}
