package ua.vintage.acceptance;

import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.MediaStore;
import android.text.InputType;
import android.webkit.CookieManager;
import android.webkit.HttpAuthHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.core.content.FileProvider;

import java.io.File;
import java.io.IOException;

public final class AcceptanceActivity extends ComponentActivity {
    private static final int PHOTO_REQUEST = 1;
    private final Uri testOrigin = Uri.parse(BuildConfig.TEST_ORIGIN);
    private WebView web;
    private ValueCallback<Uri[]> pendingPhotos;
    private Uri cameraPhoto;
    private AlertDialog authPrompt;

    @Override
    public void onCreate(Bundle state) {
        super.onCreate(state);
        WebView.setWebContentsDebuggingEnabled(false);
        web = new WebView(this);
        FrameLayout screen = new FrameLayout(this);
        screen.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });
        screen.addView(web);
        setContentView(screen);
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (web.canGoBack()) {
                    web.goBack();
                } else {
                    finish();
                }
            }
        });

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.setWebViewClient(new TestSiteClient());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> result, FileChooserParams selection) {
                if (!isTestOrigin(Uri.parse(view.getUrl() == null ? "" : view.getUrl()))) {
                    result.onReceiveValue(null);
                    return true;
                }
                finishPhotoSelection(null);
                pendingPhotos = result;
                openPhotoPicker(selection);
                return true;
            }
        });
        if (state == null || web.restoreState(state) == null) {
            web.loadUrl(BuildConfig.TEST_ORIGIN + "/mobile");
        }
    }

    private boolean isTestOrigin(Uri uri) {
        return "https".equals(uri.getScheme()) && testOrigin.getHost().equalsIgnoreCase(uri.getHost())
                && uri.getUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443);
    }

    private final class TestSiteClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri target = request.getUrl();
            if (isTestOrigin(target)) {
                return false;
            }
            if (request.isForMainFrame() && "https".equals(target.getScheme())) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, target));
                } catch (ActivityNotFoundException ignored) {
                    Toast.makeText(AcceptanceActivity.this, "Немає браузера для цього посилання.",
                            Toast.LENGTH_LONG).show();
                }
            }
            return true;
        }

        @Override
        public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
            if (!testOrigin.getHost().equalsIgnoreCase(host) || authPrompt != null) {
                handler.cancel();
                return;
            }
            EditText password = new EditText(AcceptanceActivity.this);
            password.setHint("Пароль тестового сервісу");
            password.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            authPrompt = new AlertDialog.Builder(AcceptanceActivity.this)
                    .setTitle("Доступ до Vintage UA")
                    .setView(password)
                    .setPositiveButton("Увійти", (dialog, which) -> handler.proceed("vintage", password.getText().toString()))
                    .setNegativeButton("Скасувати", (dialog, which) -> handler.cancel())
                    .setOnCancelListener(dialog -> handler.cancel())
                    .create();
            authPrompt.setOnDismissListener(dialog -> authPrompt = null);
            authPrompt.show();
        }
    }

    private void openPhotoPicker(WebChromeClient.FileChooserParams selection) {
        if (selection.isCaptureEnabled()) {
            try {
                File directory = new File(getCacheDir(), "camera");
                if (!directory.isDirectory() && !directory.mkdir()) {
                    throw new IOException("Could not create the camera cache");
                }
                File file = File.createTempFile("vintage-photo-", ".jpg", directory);
                cameraPhoto = FileProvider.getUriForFile(this, getPackageName() + ".camera", file);
                Intent capture = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
                capture.putExtra(MediaStore.EXTRA_OUTPUT, cameraPhoto);
                capture.setClipData(ClipData.newRawUri("Vintage UA", cameraPhoto));
                capture.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                startActivityForResult(capture, PHOTO_REQUEST);
                return;
            } catch (IOException | ActivityNotFoundException unavailable) {
                cameraPhoto = null;
            }
        }
        Intent gallery = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        gallery.addCategory(Intent.CATEGORY_OPENABLE);
        gallery.setType("image/*");
        gallery.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"image/jpeg", "image/png", "image/webp"});
        gallery.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, selection.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE);
        try {
            startActivityForResult(gallery, PHOTO_REQUEST);
        } catch (ActivityNotFoundException unavailable) {
            finishPhotoSelection(null);
            Toast.makeText(this, "Не вдалося відкрити галерею.", Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == PHOTO_REQUEST) {
            Uri[] selected = resultCode == RESULT_OK && cameraPhoto != null
                    ? new Uri[] {cameraPhoto} : WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            finishPhotoSelection(selected);
        }
    }

    private void finishPhotoSelection(Uri[] selected) {
        if (pendingPhotos != null) {
            pendingPhotos.onReceiveValue(selected);
            pendingPhotos = null;
        }
        cameraPhoto = null;
    }

    @Override
    protected void onPause() {
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        web.saveState(state);
        super.onSaveInstanceState(state);
    }

    @Override
    protected void onDestroy() {
        finishPhotoSelection(null);
        if (authPrompt != null) {
            authPrompt.cancel();
        }
        web.destroy();
        super.onDestroy();
    }
}
