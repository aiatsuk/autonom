package dev.autonom.helper;

import android.graphics.Bitmap;
import android.util.Base64;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;

/** A screenshot made on the device, scaled to a long edge and sent as JPEG. */
final class Shot {
    private Shot() {
    }

    static void take(Automation automation, int maxEdge, int quality, JSONObject reply)
            throws HelperError, JSONException {
        Bitmap full = automation.ui().takeScreenshot();
        if (full == null) {
            throw new HelperError("screenshot_failed", "The device returned no screenshot");
        }
        Bitmap image = full;
        try {
            int width = full.getWidth();
            int height = full.getHeight();
            int longEdge = Math.max(width, height);
            if (longEdge > maxEdge) {
                // never upscaled: only a larger screen is scaled down
                double scale = maxEdge / (double) longEdge;
                int scaledWidth = Math.max(1, (int) Math.round(width * scale));
                int scaledHeight = Math.max(1, (int) Math.round(height * scale));
                image = Bitmap.createScaledBitmap(full, scaledWidth, scaledHeight, true);
            }
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            if (!image.compress(Bitmap.CompressFormat.JPEG, quality, bytes)) {
                throw new HelperError("screenshot_failed", "The screenshot could not be encoded as JPEG");
            }
            int screenWidth = width;
            int screenHeight = height;
            try {
                Automation.DisplayInfo display = Automation.display();
                screenWidth = display.width;
                screenHeight = display.height;
            } catch (HelperError ignored) {
                // the bitmap size is the screen size then
            }
            reply.put("format", "jpeg");
            reply.put("width", image.getWidth());
            reply.put("height", image.getHeight());
            reply.put("screen_width", screenWidth);
            reply.put("screen_height", screenHeight);
            reply.put("data_base64", Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP));
        } finally {
            if (image != full) {
                image.recycle();
            }
            full.recycle();
        }
    }
}
