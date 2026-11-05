package dev.autonom.helper;

import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Build;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.LinkedHashSet;

/**
 * Autonom's on-device helper, protocol 1.
 *
 * <p>Started by the host with {@code app_process} under the process name
 * {@code autonom-helper}:
 * {@code Main --build <id> --idle-ms <ms> --socket autonom-helper}. It holds
 * one UiAutomation connection and serves JSON lines over the abstract local
 * socket, one connection at a time. Every request is
 * {@code {"id": "...", "op": "...", ...}} and gets one reply line with the
 * same id and {@code "ok"}. The first request of a connection must be
 * {@code hello}; an id seen among the last 256 is refused as
 * {@code duplicate_id} and not performed. After {@code --idle-ms} without a
 * request the process releases UiAutomation and exits.
 *
 * <p>The log (standard output) has start and exit lines only, never request
 * content.
 */
public final class Main {
    static final int PROTOCOL = 1;
    static final String DEFAULT_SOCKET = "autonom-helper";
    static final long DEFAULT_IDLE_MS = 120000;
    static final int RECENT_IDS = 256;
    static final int MAX_ID_LENGTH = 64;
    /** A connection that sends nothing for this long is closed for the next one. */
    static final int CONNECTION_QUIET_MS = 30000;
    static final int MAX_WAIT_MS = 60000;

    private final String build;
    private final long idleMs;
    private final String socketName;
    private final Automation automation = new Automation();
    private final Input input = new Input(automation);
    private HelperError automationError;
    private final LinkedHashSet<String> recentIds = new LinkedHashSet<>();

    private final Object activity = new Object();
    private long lastRequest = SystemClock.uptimeMillis();
    private int busy;
    private boolean exiting;

    private Main(String build, long idleMs, String socketName) {
        this.build = build;
        this.idleMs = idleMs;
        this.socketName = socketName;
    }

    @SuppressWarnings("deprecation") // prepareMainLooper: the documented way for a process without an app
    public static void main(String[] args) {
        String build = "unknown";
        long idleMs = DEFAULT_IDLE_MS;
        String socket = DEFAULT_SOCKET;
        try {
            for (int i = 0; i < args.length; i++) {
                String name = args[i];
                if (i + 1 >= args.length) {
                    throw new IllegalArgumentException(name + " needs a value");
                }
                String value = args[++i];
                if ("--build".equals(name)) {
                    build = value;
                } else if ("--idle-ms".equals(name)) {
                    idleMs = Long.parseLong(value);
                    if (idleMs < 1000) {
                        throw new IllegalArgumentException("--idle-ms must be at least 1000");
                    }
                } else if ("--socket".equals(name)) {
                    socket = value;
                } else {
                    throw new IllegalArgumentException("unknown option " + name);
                }
            }
        } catch (RuntimeException e) {
            System.out.println("autonom-helper exit usage: " + e.getMessage());
            System.exit(2);
            return;
        }
        // AccessibilityInteractionClient needs a main looper in this process.
        Looper.prepareMainLooper();
        new Main(build, idleMs, socket).run();
    }

    private static void log(String line) {
        System.out.println("autonom-helper " + line);
        System.out.flush();
    }

    private void run() {
        log("start build=" + build + " protocol=" + PROTOCOL + " pid=" + Process.myPid()
                + " sdk=" + Build.VERSION.SDK_INT + " idle_ms=" + idleMs);
        LocalServerSocket server;
        try {
            server = new LocalServerSocket(socketName);
        } catch (IOException e) {
            // Another helper serves this name; never take UiAutomation from it.
            log("exit socket_in_use");
            System.exit(1);
            return;
        }
        try {
            automation.connect();
            log("uiautomation connected");
        } catch (HelperError e) {
            automationError = e;
            log("uiautomation " + e.code);
        }
        startIdleWatch();
        while (true) {
            LocalSocket client;
            try {
                client = server.accept();
            } catch (IOException e) {
                finish("accept_failed", 1);
                return;
            }
            boolean stop = false;
            try {
                stop = serve(client);
            } catch (IOException | RuntimeException e) {
                // the host went away or sent something unreadable; next connection
            } finally {
                try {
                    client.close();
                } catch (IOException ignored) {
                    // already closed
                }
            }
            if (stop) {
                finish(automationError != null ? "uiautomation_unavailable" : "quit",
                        automationError != null ? 3 : 0);
                return;
            }
        }
    }

    private void startIdleWatch() {
        Thread watch = new Thread(new Runnable() {
            @Override
            public void run() {
                while (true) {
                    SystemClock.sleep(250);
                    synchronized (activity) {
                        if (exiting) {
                            return;
                        }
                        if (busy > 0 || SystemClock.uptimeMillis() - lastRequest < idleMs) {
                            continue;
                        }
                        exiting = true;
                    }
                    finish("idle", 0);
                    return;
                }
            }
        }, "autonom-helper-idle");
        watch.setDaemon(true);
        watch.start();
    }

    /** Releases UiAutomation and ends the process. */
    private void finish(String reason, int status) {
        synchronized (activity) {
            exiting = true;
        }
        automation.disconnect();
        log("exit " + reason);
        System.exit(status);
    }

    private boolean begin() {
        synchronized (activity) {
            if (exiting) {
                return false;
            }
            busy++;
            lastRequest = SystemClock.uptimeMillis();
            return true;
        }
    }

    private void end() {
        synchronized (activity) {
            busy--;
            lastRequest = SystemClock.uptimeMillis();
        }
    }

    /** Per-connection state. */
    private static final class Connection {
        boolean greeted;
        /** Set when the reply just written ends the process. */
        boolean stop;
    }

    /** Serves one connection; true when the process must end after it. */
    private boolean serve(LocalSocket client) throws IOException {
        client.setSoTimeout((int) Math.min(CONNECTION_QUIET_MS, idleMs));
        BufferedReader in = new BufferedReader(
                new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
        Writer out = new BufferedWriter(
                new OutputStreamWriter(client.getOutputStream(), StandardCharsets.UTF_8));
        Connection connection = new Connection();
        String line;
        while ((line = in.readLine()) != null) {
            if (line.trim().isEmpty()) {
                continue;
            }
            if (!begin()) {
                return false;
            }
            String reply;
            try {
                JSONObject result = handle(line, connection);
                reply = result.toString();
                if (reply == null) { // org.json returns null for a value it cannot encode
                    Object id = result.opt("id");
                    reply = error(id instanceof String ? (String) id : null, "internal",
                            "The reply could not be encoded").toString();
                }
            } finally {
                end();
            }
            out.write(reply);
            out.write('\n');
            out.flush();
            if (connection.stop) {
                return true;
            }
        }
        return false;
    }

    private JSONObject handle(String line, Connection connection) {
        JSONObject request;
        try {
            request = new JSONObject(line);
        } catch (JSONException e) {
            return error(null, "bad_request", "A request is one JSON object per line");
        }
        Object rawId = request.opt("id");
        if (!(rawId instanceof String) || ((String) rawId).isEmpty()
                || ((String) rawId).length() > MAX_ID_LENGTH) {
            return error(null, "bad_request", "id must be a string of 1-" + MAX_ID_LENGTH + " characters");
        }
        String id = (String) rawId;
        if (recentIds.contains(id)) {
            return error(id, "duplicate_id", "This id was already received; the request was not performed");
        }
        remember(id);
        Object rawOp = request.opt("op");
        if (!(rawOp instanceof String)) {
            return error(id, "bad_request", "op must be a string");
        }
        String op = (String) rawOp;
        if (!connection.greeted && !"hello".equals(op)) {
            return error(id, "hello_required", "The first request of a connection must be hello");
        }
        try {
            JSONObject reply = new JSONObject();
            reply.put("id", id);
            reply.put("ok", true);
            dispatch(op, request, reply, connection);
            return reply;
        } catch (HelperError e) {
            JSONObject reply = error(id, e.code, e.getMessage());
            if ("hello".equals(op)) {
                identify(reply);
            }
            return reply;
        } catch (Throwable t) {
            return error(id, "internal", t.getClass().getName() + ": " + t.getMessage());
        }
    }

    private void remember(String id) {
        recentIds.add(id);
        if (recentIds.size() > RECENT_IDS) {
            Iterator<String> oldest = recentIds.iterator();
            oldest.next();
            oldest.remove();
        }
    }

    private static JSONObject error(String id, String code, String message) {
        JSONObject reply = new JSONObject();
        try {
            reply.put("id", id == null ? JSONObject.NULL : id);
            reply.put("ok", false);
            reply.put("error", errorBody(code, message));
        } catch (JSONException impossible) {
            throw new IllegalStateException(impossible);
        }
        return reply;
    }

    /** {@code {"code": ..., "message": ...}}, as in an error reply. */
    private static JSONObject errorBody(String code, String message) {
        JSONObject error = new JSONObject();
        try {
            error.put("code", code);
            error.put("message", message == null ? code : message);
        } catch (JSONException impossible) {
            throw new IllegalStateException(impossible);
        }
        return error;
    }

    private void identify(JSONObject reply) {
        try {
            reply.put("protocol", PROTOCOL);
            reply.put("build", build);
            reply.put("pid", Process.myPid());
            reply.put("sdk_int", Build.VERSION.SDK_INT);
        } catch (JSONException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private void dispatch(String op, JSONObject request, JSONObject reply, Connection connection)
            throws HelperError, JSONException, IOException {
        switch (op) {
            case "hello":
                if (automationError != null) {
                    // Nothing else can work: answer, then end the process.
                    connection.stop = true;
                    throw automationError;
                }
                connection.greeted = true;
                identify(reply);
                reply.put("uiautomation", "connected");
                return;
            case "read": {
                int quietMs = intArg(request, "quiet_ms", 500, 0, MAX_WAIT_MS);
                int maxMs = intArg(request, "max_ms", 2000, 0, MAX_WAIT_MS);
                Automation.Settle quiet = automation.waitQuiet(quietMs, maxMs);
                JSONObject snapshot = snapshot();
                reply.put("xml", snapshot.get("xml"));
                reply.put("settled", quiet.settled);
                reply.put("settle_ms", quiet.settleMs);
                putBusy(reply, quiet);
                reply.put("windows", snapshot.get("windows"));
                reply.put("display", snapshot.get("display"));
                return;
            }
            case "display": {
                Automation.DisplayInfo display = Automation.display();
                reply.put("width", display.width);
                reply.put("height", display.height);
                reply.put("rotation", display.rotation);
                return;
            }
            case "tap": {
                final float x = coordinate(request, "x");
                final float y = coordinate(request, "y");
                perform(request, reply, true, new Action() {
                    @Override
                    public void run() throws HelperError {
                        input.tap(x, y);
                    }
                });
                return;
            }
            case "long_press": {
                final float x = coordinate(request, "x");
                final float y = coordinate(request, "y");
                final int duration = intArg(request, "duration_ms", 1000, 1, MAX_WAIT_MS);
                perform(request, reply, true, new Action() {
                    @Override
                    public void run() throws HelperError {
                        input.longPress(x, y, duration);
                    }
                });
                return;
            }
            case "swipe": {
                final float x1 = coordinate(request, "x1");
                final float y1 = coordinate(request, "y1");
                final float x2 = coordinate(request, "x2");
                final float y2 = coordinate(request, "y2");
                final int duration = intArg(request, "duration_ms", 300, 1, MAX_WAIT_MS);
                perform(request, reply, true, new Action() {
                    @Override
                    public void run() throws HelperError {
                        input.swipe(x1, y1, x2, y2, duration);
                    }
                });
                return;
            }
            case "key": {
                final int keyCode = intArg(request, "keycode", -1, 1, KeyEvent.getMaxKeyCode());
                if (keyCode < 0) {
                    throw HelperError.badRequest("keycode is required");
                }
                perform(request, reply, true, new Action() {
                    @Override
                    public void run() throws HelperError {
                        input.key(keyCode);
                    }
                });
                return;
            }
            case "type": {
                final String text = text(request, "text");
                if (text.isEmpty()) {
                    throw HelperError.badRequest("text must not be empty");
                }
                final Input.Typed[] typed = new Input.Typed[1];
                perform(request, reply, false, new Action() {
                    @Override
                    public void run() throws HelperError {
                        typed[0] = input.type(text);
                    }
                });
                reply.put("method", typed[0].method);
                putTyped(reply, typed[0]);
                return;
            }
            case "set_text": {
                final String text = text(request, "text");
                final int[] expected = bounds(request, "expect_bounds");
                final Input.Typed[] after = new Input.Typed[1];
                perform(request, reply, false, new Action() {
                    @Override
                    public void run() throws HelperError {
                        AccessibilityNodeInfo field = input.editableFocus();
                        if (expected != null) {
                            Automation.DisplayInfo display = Automation.display();
                            android.graphics.Rect actual =
                                    TreeXml.visibleBounds(field, display.width, display.height);
                            if (!Input.boundsMatch(actual, expected)) {
                                throw new HelperError("fill_target_mismatch",
                                        "The focused field is at " + actual.toShortString()
                                                + ", not at the expected bounds");
                            }
                        }
                        after[0] = input.replace(field, text);
                    }
                });
                putTyped(reply, after[0]);
                return;
            }
            case "screenshot": {
                int maxEdge = intArg(request, "max_edge", 1000, 16, 100000);
                int quality = intArg(request, "quality", 80, 1, 100);
                Shot.take(automation, maxEdge, quality, reply);
                return;
            }
            case "quit":
                connection.stop = true;
                return;
            default:
                throw new HelperError("unknown_op", "Unknown op for protocol " + PROTOCOL);
        }
    }

    /** `text_after` and `complete` of a type or set_text reply (null when unknown). */
    private static void putTyped(JSONObject reply, Input.Typed typed) throws JSONException {
        reply.put("text_after", typed.textAfter == null ? JSONObject.NULL : typed.textAfter);
        reply.put("complete", typed.complete == null ? JSONObject.NULL : typed.complete);
    }

    /** `busy` for a wait that did not reach quiet; nothing when it did. */
    private static void putBusy(JSONObject reply, Automation.Settle result) throws JSONException {
        if (!result.settled && result.busy != null) {
            reply.put("busy", result.busy.toJson());
        }
    }

    /** One input: performed once, then (when asked) settled and read. */
    private interface Action {
        void run() throws HelperError;
    }

    private void perform(JSONObject request, JSONObject reply, boolean settleByDefault, Action action)
            throws HelperError, JSONException, IOException {
        boolean readAfter = bool(request, "read_after");
        Object settle = request.opt("settle");
        boolean settleOn;
        int firstEventMs = 300;
        int quietMs = 500;
        int maxMs = 2000;
        if (settle == null || settle == JSONObject.NULL) {
            settleOn = settleByDefault || readAfter;
        } else if (settle instanceof Boolean) {
            settleOn = (Boolean) settle;
        } else if (settle instanceof JSONObject) {
            JSONObject options = (JSONObject) settle;
            settleOn = true;
            firstEventMs = intArg(options, "first_event_ms", firstEventMs, 0, MAX_WAIT_MS);
            quietMs = intArg(options, "quiet_ms", quietMs, 0, MAX_WAIT_MS);
            maxMs = intArg(options, "max_ms", maxMs, 0, MAX_WAIT_MS);
        } else {
            throw HelperError.badRequest("settle must be an object or a boolean");
        }
        long since = SystemClock.uptimeMillis();
        if (settleOn) {
            automation.mark(since);
        }
        try {
            action.run();
        } catch (HelperError | RuntimeException e) {
            automation.clearMark();
            throw e;
        }
        // Every settle limit counts from here, the end of the input; waiting
        // for the app to handle it is part of the settle and within max_ms.
        long done = SystemClock.uptimeMillis();
        reply.put("performed", true);
        if (settleOn) {
            Automation.Settle result = automation.settle(done, firstEventMs, quietMs, maxMs);
            reply.put("settled", result.settled);
            reply.put("settle_ms", result.settleMs);
            reply.put("first_event_ms", result.firstEventMs < 0 ? JSONObject.NULL : result.firstEventMs);
            putBusy(reply, result);
        }
        if (readAfter) {
            // The input happened: a failed read must not turn the reply into
            // a failure, or the host could take the input for not sent.
            try {
                reply.put("read", snapshot());
            } catch (HelperError e) {
                reply.put("read_error", errorBody(e.code, e.getMessage()));
            } catch (IOException | JSONException | RuntimeException e) {
                reply.put("read_error", errorBody("internal", e.getClass().getName() + ": " + e.getMessage()));
            }
        }
    }

    /**
     * The screen now: XML of the active window (read with window tracking
     * off, as `uiautomator dump` reads it), every window, the display.
     */
    private JSONObject snapshot() throws HelperError, JSONException, IOException {
        automation.clearCache();
        Automation.DisplayInfo display = Automation.display();
        String xml;
        automation.pauseWindowTracking();
        try {
            xml = TreeXml.hierarchy(automation.activeRoot(), display);
        } finally {
            automation.resumeWindowTracking();
        }
        JSONObject snapshot = new JSONObject();
        snapshot.put("xml", xml);
        snapshot.put("windows", TreeXml.windows(automation.windows()));
        JSONObject size = new JSONObject();
        size.put("width", display.width);
        size.put("height", display.height);
        size.put("rotation", display.rotation);
        snapshot.put("display", size);
        return snapshot;
    }

    // --- arguments ----------------------------------------------------------

    private static int intArg(JSONObject request, String key, int fallback, int min, int max)
            throws HelperError {
        Object value = request.opt(key);
        if (value == null || value == JSONObject.NULL) {
            return fallback;
        }
        if (!(value instanceof Number)) {
            throw HelperError.badRequest(key + " must be a whole number");
        }
        double number = ((Number) value).doubleValue();
        if (number != Math.floor(number) || number < min || number > max) {
            throw HelperError.badRequest(key + " must be a whole number from " + min + " to " + max);
        }
        return (int) number;
    }

    private static float coordinate(JSONObject request, String key) throws HelperError {
        Object value = request.opt(key);
        if (!(value instanceof Number)) {
            throw HelperError.badRequest(key + " must be a number");
        }
        double number = ((Number) value).doubleValue();
        if (Double.isNaN(number) || Double.isInfinite(number) || Math.abs(number) > 1000000) {
            throw HelperError.badRequest(key + " must be a finite screen coordinate");
        }
        return (float) number;
    }

    private static String text(JSONObject request, String key) throws HelperError {
        Object value = request.opt(key);
        if (!(value instanceof String)) {
            throw HelperError.badRequest(key + " must be a string");
        }
        return (String) value;
    }

    private static boolean bool(JSONObject request, String key) throws HelperError {
        Object value = request.opt(key);
        if (value == null || value == JSONObject.NULL) {
            return false;
        }
        if (!(value instanceof Boolean)) {
            throw HelperError.badRequest(key + " must be true or false");
        }
        return (Boolean) value;
    }

    private static int[] bounds(JSONObject request, String key) throws HelperError {
        Object value = request.opt(key);
        if (value == null || value == JSONObject.NULL) {
            return null;
        }
        if (!(value instanceof JSONArray) || ((JSONArray) value).length() != 4) {
            throw HelperError.badRequest(key + " must be [left, top, right, bottom]");
        }
        JSONArray array = (JSONArray) value;
        int[] result = new int[4];
        for (int i = 0; i < 4; i++) {
            Object item = array.opt(i);
            if (!(item instanceof Number) || ((Number) item).doubleValue() != Math.floor(((Number) item).doubleValue())) {
                throw HelperError.badRequest(key + " must hold four whole numbers");
            }
            result[i] = ((Number) item).intValue();
        }
        return result;
    }
}
