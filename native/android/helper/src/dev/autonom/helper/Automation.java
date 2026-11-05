package dev.autonom.helper;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.content.Context;
import android.graphics.Point;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Display;
import android.view.InputEvent;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * The one UiAutomation connection this process holds, made the way a shell
 * tool makes it: the hidden constructor and connect method are reached by
 * reflection (they are not in the public SDK), on a looper of our own thread.
 * An event listener time-stamps accessibility events, so an input can wait
 * for the screen to start changing before it waits for quiet.
 *
 * <p>A settle or read that does not reach quiet reports what kept it busy
 * (see {@link Busy}): the events that counted against quiet while it waited.
 * Reporting only; what counts for quiet is the same either way.
 *
 * <p>Window tracking (FLAG_RETRIEVE_INTERACTIVE_WINDOWS) is on, so the
 * windows list and input focus work across windows, except while the XML tree
 * is read: with tracking on, the system marks every node outside the part of
 * its window that no other window covers as invisible (a list row under the
 * navigation bar, a row with empty bounds), and `uiautomator dump`, which
 * does not track windows, keeps those nodes.
 */
final class Automation {
    /** UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES (API 24). */
    private static final int FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES = 1;
    private static final long ROOT_RETRY_MS = 1000;
    /**
     * Window-change events dated this close after our own tracking switch
     * are the system reporting the switch, not the screen changing.
     */
    private static final long OWN_SWITCH_MARGIN_MS = 250;
    private static final int SWITCH_HISTORY = 4;
    /** Events kept for a wait that starts slightly after its origin. */
    private static final int RECENT_EVENTS = 32;

    private final HandlerThread thread = new HandlerThread("autonom-helper-automation");
    private volatile UiAutomation automation;
    private Method clearCache;
    private boolean clearCacheLooked;
    private Method syncInput;
    private boolean syncInputTakesFlag;
    private boolean syncInputLooked;
    /** Whether FLAG_RETRIEVE_INTERACTIVE_WINDOWS is set on the connection now. */
    private boolean windowTracking;

    // Event bookkeeping, guarded by `events`.
    private final Object events = new Object();
    private long markSince = Long.MAX_VALUE;
    private long firstEventAfterMark = -1;
    /** When the latest event that counts for quiet arrived (uptime ms). */
    private long lastEventAt;
    /** Recent periods of our own tracking switches: [from, to] in uptime ms. */
    private final long[] switchFrom = new long[SWITCH_HISTORY];
    private final long[] switchTo = new long[SWITCH_HISTORY];
    private int switchNext;
    private int switchOpen = -1;
    /** The latest events that count for quiet: arrival (uptime ms), package, class, type. */
    private final long[] recentAt = new long[RECENT_EVENTS];
    private final String[] recentPackage = new String[RECENT_EVENTS];
    private final String[] recentClass = new String[RECENT_EVENTS];
    private final int[] recentType = new int[RECENT_EVENTS];
    private int recentNext;
    private int recentCount;
    /** The wait that is counting events now, or null. */
    private Busy busy;

    // The round trip to the app after an input (see awaitApp), guarded by `appSync`.
    private final Object appSync = new Object();
    private Thread appThread;
    /** The latest round trip asked for, and the latest one the app answered. */
    private long appAsked;
    private long appAnswered;

    /** Result of a settle or read wait: whether quiet was reached and how long it took. */
    static final class Settle {
        final boolean settled;
        final long settleMs;
        /** Milliseconds from the end of the input to its first event, or -1. */
        final long firstEventMs;
        /** What kept the wait from quiet; null when it settled. */
        final Busy busy;

        Settle(boolean settled, long settleMs, long firstEventMs, Busy busy) {
            this.settled = settled;
            this.settleMs = settleMs;
            this.firstEventMs = firstEventMs;
            this.busy = settled ? null : busy;
        }
    }

    /**
     * The accessibility events that counted against one wait for quiet (the
     * ones that count for quiet, so not the window changes of our own
     * tracking switches): how many, how many per package, and the last few
     * with their arrival in ms from the wait's origin (the same origin as
     * `settle_ms`; negative for an event shortly before a read began, which
     * still counted against its quiet).
     */
    static final class Busy {
        static final int LAST = 8;
        final long origin;
        /** Events that arrived from this time on are counted. */
        final long from;
        private int count;
        private final Map<String, Integer> packages = new HashMap<>();
        private final long[] lastAt = new long[LAST];
        private final String[] lastPackage = new String[LAST];
        private final String[] lastClass = new String[LAST];
        private final int[] lastType = new int[LAST];
        private int lastNext;
        private int lastCount;
        /** False when the app of the active window had not answered by the end of the wait. */
        boolean appAnswered = true;

        Busy(long origin, long from) {
            this.origin = origin;
            this.from = from;
        }

        void add(long at, String pkg, String cls, int type) {
            count++;
            String key = pkg == null ? "" : pkg;
            Integer seen = packages.get(key);
            packages.put(key, seen == null ? 1 : seen + 1);
            lastAt[lastNext] = at;
            lastPackage[lastNext] = pkg;
            lastClass[lastNext] = cls;
            lastType[lastNext] = type;
            lastNext = (lastNext + 1) % LAST;
            if (lastCount < LAST) {
                lastCount++;
            }
        }

        /**
         * {@code {"events": n, "last": [{"package", "class", "type", "ms"}, ...],
         * "packages": {"<package>": n, ...}}}, packages by count, most first
         * ("" for events without a package), plus {@code "app_answered": false}
         * when the app never answered the round trip after the input.
         */
        JSONObject toJson() throws JSONException {
            JSONObject json = new JSONObject();
            json.put("events", count);
            if (!appAnswered) {
                json.put("app_answered", false);
            }
            JSONArray last = new JSONArray();
            for (int i = 0; i < lastCount; i++) {
                int slot = (lastNext - lastCount + i + LAST) % LAST;
                JSONObject event = new JSONObject();
                event.put("package", lastPackage[slot] == null ? JSONObject.NULL : lastPackage[slot]);
                event.put("class", lastClass[slot] == null ? JSONObject.NULL : lastClass[slot]);
                event.put("type", AccessibilityEvent.eventTypeToString(lastType[slot]));
                event.put("ms", lastAt[slot] - origin);
                last.put(event);
            }
            json.put("last", last);
            List<Map.Entry<String, Integer>> entries = new ArrayList<>(packages.entrySet());
            Collections.sort(entries, new Comparator<Map.Entry<String, Integer>>() {
                @Override
                public int compare(Map.Entry<String, Integer> a, Map.Entry<String, Integer> b) {
                    int byCount = b.getValue().compareTo(a.getValue());
                    return byCount != 0 ? byCount : a.getKey().compareTo(b.getKey());
                }
            });
            JSONObject byPackage = new JSONObject(); // keeps insertion order on Android
            for (Map.Entry<String, Integer> entry : entries) {
                byPackage.put(entry.getKey(), entry.getValue());
            }
            json.put("packages", byPackage);
            return json;
        }
    }

    /** Display size as input coordinates see it now, and its rotation. */
    static final class DisplayInfo {
        final int width;
        final int height;
        final int rotation;

        DisplayInfo(int width, int height, int rotation) {
            this.width = width;
            this.height = height;
            this.rotation = rotation;
        }
    }

    Automation() {
        for (int i = 0; i < SWITCH_HISTORY; i++) {
            switchFrom[i] = Long.MAX_VALUE;
            switchTo[i] = Long.MIN_VALUE;
        }
    }

    void connect() throws HelperError {
        thread.start();
        UiAutomation created;
        try {
            Class<?> connectionType = Class.forName("android.app.IUiAutomationConnection");
            Object connection = Class.forName("android.app.UiAutomationConnection")
                    .getDeclaredConstructor().newInstance();
            created = construct(connectionType, connection);
            try {
                Method connect = UiAutomation.class.getDeclaredMethod("connect", int.class);
                connect.setAccessible(true);
                connect.invoke(created, FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            } catch (NoSuchMethodException e) {
                Method connect = UiAutomation.class.getDeclaredMethod("connect");
                connect.setAccessible(true);
                connect.invoke(created);
            }
        } catch (InvocationTargetException e) {
            throw classify(e.getCause() != null ? e.getCause() : e);
        } catch (Throwable t) {
            throw classify(t);
        }
        try {
            AccessibilityServiceInfo info = created.getServiceInfo();
            if (info == null) {
                throw new HelperError("uiautomation_failed", "UiAutomation connected without service info");
            }
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            created.setServiceInfo(info);
            windowTracking = true;
            synchronized (events) {
                lastEventAt = SystemClock.uptimeMillis();
            }
            created.setOnAccessibilityEventListener(new UiAutomation.OnAccessibilityEventListener() {
                @Override
                public void onAccessibilityEvent(AccessibilityEvent event) {
                    noteEvent(event);
                }
            });
        } catch (RuntimeException e) {
            throw classify(e);
        }
        automation = created;
    }

    private UiAutomation construct(Class<?> connectionType, Object connection) throws Exception {
        try {
            Constructor<UiAutomation> constructor =
                    UiAutomation.class.getDeclaredConstructor(Looper.class, connectionType);
            constructor.setAccessible(true);
            return constructor.newInstance(thread.getLooper(), connection);
        } catch (NoSuchMethodException e) {
            Constructor<UiAutomation> constructor =
                    UiAutomation.class.getDeclaredConstructor(Context.class, connectionType);
            constructor.setAccessible(true);
            return constructor.newInstance(null, connection);
        }
    }

    /** Another UiAutomation user (uiautomator, uiautomator2, Appium) is "busy". */
    private static HelperError classify(Throwable failure) {
        for (Throwable t = failure; t != null; t = t.getCause()) {
            String message = String.valueOf(t.getMessage());
            if (message.contains("already registered")) {
                return new HelperError("uiautomation_busy",
                        "Another tool holds UiAutomation on this device: " + message);
            }
        }
        return new HelperError("uiautomation_failed",
                "UiAutomation could not connect: " + failure.getClass().getName() + ": "
                        + failure.getMessage());
    }

    void disconnect() {
        UiAutomation current = automation;
        automation = null;
        if (current == null) {
            return;
        }
        try {
            current.setOnAccessibilityEventListener(null);
            Method disconnect = UiAutomation.class.getDeclaredMethod("disconnect");
            disconnect.setAccessible(true);
            disconnect.invoke(current);
        } catch (Throwable ignored) {
            // The system drops the registration when this process dies anyway.
        }
    }

    UiAutomation ui() throws HelperError {
        UiAutomation current = automation;
        if (current == null) {
            throw new HelperError("uiautomation_failed", "UiAutomation is not connected");
        }
        return current;
    }

    // --- events ---------------------------------------------------------------

    private void noteEvent(AccessibilityEvent event) {
        long now = SystemClock.uptimeMillis();
        long time = event.getEventTime();
        if (time <= 0 || time > now) {
            time = now;
        }
        int type = event.getEventType();
        CharSequence pkg = event.getPackageName();
        CharSequence cls = event.getClassName();
        synchronized (events) {
            if (type == AccessibilityEvent.TYPE_WINDOWS_CHANGED && ownSwitch(time)) {
                return;
            }
            // Arrival time, not event time: an event that arrives late still
            // means the screen changed just now as far as a read can tell.
            if (now > lastEventAt) {
                lastEventAt = now;
            }
            if (firstEventAfterMark < 0 && time >= markSince) {
                firstEventAfterMark = time;
            }
            remember(now, pkg == null ? null : pkg.toString(), cls == null ? null : cls.toString(), type);
            events.notifyAll();
        }
    }

    /** Keeps an event that counts for quiet, for the wait counting now. Holds `events`. */
    private void remember(long at, String pkg, String cls, int type) {
        recentAt[recentNext] = at;
        recentPackage[recentNext] = pkg;
        recentClass[recentNext] = cls;
        recentType[recentNext] = type;
        recentNext = (recentNext + 1) % RECENT_EVENTS;
        if (recentCount < RECENT_EVENTS) {
            recentCount++;
        }
        if (busy != null && at >= busy.from) {
            busy.add(at, pkg, cls, type);
        }
    }

    /**
     * Starts counting the events of a wait whose time counts from `origin`:
     * every event that arrives from `from` on (at or before `origin`),
     * including those that already came.
     */
    private void openBusy(long origin, long from) {
        synchronized (events) {
            busy = new Busy(origin, from);
            for (int i = 0; i < recentCount; i++) {
                int slot = (recentNext - recentCount + i + RECENT_EVENTS) % RECENT_EVENTS;
                if (recentAt[slot] >= from) {
                    busy.add(recentAt[slot], recentPackage[slot], recentClass[slot], recentType[slot]);
                }
            }
        }
    }

    private Busy closeBusy() {
        synchronized (events) {
            Busy done = busy;
            busy = null;
            return done;
        }
    }

    /** True for a time within one of our recent tracking switches. Holds `events`. */
    private boolean ownSwitch(long time) {
        for (int i = 0; i < SWITCH_HISTORY; i++) {
            if (time >= switchFrom[i] && time - OWN_SWITCH_MARGIN_MS <= switchTo[i]) {
                return true;
            }
        }
        return false;
    }

    /** Starts counting events from `since` (taken right before an input). */
    void mark(long since) {
        synchronized (events) {
            markSince = since;
            firstEventAfterMark = -1;
        }
    }

    void clearMark() {
        synchronized (events) {
            markSince = Long.MAX_VALUE;
            firstEventAfterMark = -1;
        }
    }

    /**
     * After an input that ended at `done`, all within `maxMs` of `done`:
     * wait until the app of the active window has handled it (see
     * {@link #awaitApp}), let a window transition it started finish (as
     * input injection itself does), wait up to `firstEventMs` for the first
     * accessibility event caused by it, then until `quietMs` passed without
     * events, counted from the app's answer at the earliest. An app that has
     * not answered by the limit is not settled. The transition wait never
     * takes the time the quiet wait needs.
     */
    Settle settle(long done, int firstEventMs, int quietMs, int maxMs) {
        long deadline = done + maxMs;
        openBusy(done, done); // events from the input on count against quiet
        long answered = awaitApp(deadline);
        if (answered < 0) {
            // The app is still busy with the input: there is no quiet to wait for.
            long first;
            synchronized (events) {
                first = firstEventAfterMark;
                markSince = Long.MAX_VALUE;
                firstEventAfterMark = -1;
            }
            long settleMs = Math.max(0, SystemClock.uptimeMillis() - done);
            Busy counted = closeBusy();
            if (counted != null) {
                counted.appAnswered = false;
            }
            return new Settle(false, settleMs, first < 0 ? -1 : Math.max(0, first - done), counted);
        }
        waitForTransitions(deadline - quietMs - SystemClock.uptimeMillis());
        long first;
        synchronized (events) {
            long firstDeadline = Math.min(answered + firstEventMs, deadline);
            while (firstEventAfterMark < 0) {
                long wait = firstDeadline - SystemClock.uptimeMillis();
                if (wait <= 0) {
                    break;
                }
                try {
                    events.wait(wait);
                } catch (InterruptedException ignored) {
                    // keep waiting until the deadline
                }
            }
            first = firstEventAfterMark;
            markSince = Long.MAX_VALUE;
            firstEventAfterMark = -1;
        }
        // Before the answer the app could not report changes: that was not quiet.
        boolean settled = waitQuietUntil(quietMs, deadline, answered);
        long settleMs = Math.max(0, SystemClock.uptimeMillis() - done);
        Busy counted = closeBusy();
        long firstMs = first < 0 ? -1 : Math.max(0, first - done);
        return new Settle(settled, settleMs, firstMs, counted);
    }

    /**
     * Waits until no event came for `quietMs` (settled) or `maxMs` ran out
     * first (not settled, with what kept it busy).
     */
    Settle waitQuiet(long quietMs, long maxMs) {
        long started = SystemClock.uptimeMillis();
        // an event up to quietMs before the read still keeps it from quiet
        openBusy(started, started - Math.max(0, quietMs));
        boolean settled = waitQuietUntil(quietMs, started + Math.max(0, maxMs), Long.MIN_VALUE);
        long settleMs = Math.max(0, SystemClock.uptimeMillis() - started);
        return new Settle(settled, settleMs, -1, closeBusy());
    }

    /**
     * True once `quietMs` passed without events, counted from `since` at the
     * earliest; false when `maxMs` (from now) ran out first.
     */
    boolean quietSince(long since, long quietMs, long maxMs) {
        return waitQuietUntil(quietMs, SystemClock.uptimeMillis() + Math.max(0, maxMs), since);
    }

    /**
     * True once `quietMs` passed without events since the later of the last
     * event and `notBefore`; false when `deadline` comes first.
     */
    private boolean waitQuietUntil(long quietMs, long deadline, long notBefore) {
        synchronized (events) {
            while (true) {
                long now = SystemClock.uptimeMillis();
                long quietFor = now - Math.max(lastEventAt, notBefore);
                if (quietFor >= quietMs) {
                    return true;
                }
                long wait = Math.min(quietMs - quietFor, deadline - now);
                if (wait <= 0) {
                    return false;
                }
                try {
                    events.wait(wait);
                } catch (InterruptedException ignored) {
                    // keep waiting until the deadline
                }
            }
        }
    }

    // --- after an input ---------------------------------------------------------

    /**
     * One accessibility round trip to the app of the active window, waited
     * for until `deadline` (uptime ms): the time it came back, or -1 when the
     * app had not answered by then. The app answers it on its main thread
     * after the work the input queued there, so a click handler has run
     * (Android sends the click event only after the handler returns, and a
     * handler that starts an activity can take longer than the first-event
     * wait on a busy device). The round trip runs on a thread of its own,
     * because the system waits up to its own interaction timeout (about 5 s)
     * for a busy app; a round trip still running when the next one is asked
     * for is waited for first, then a new one is made. A round trip that
     * fails (no active window) counts as answered: nothing more to wait for.
     */
    long awaitApp(long deadline) {
        synchronized (appSync) {
            if (SystemClock.uptimeMillis() >= deadline) {
                return SystemClock.uptimeMillis(); // no time left to ask
            }
            long ticket = ++appAsked;
            if (appThread == null) {
                appThread = new Thread(new Runnable() {
                    @Override
                    public void run() {
                        appRoundTrips();
                    }
                }, "autonom-helper-app");
                appThread.setDaemon(true);
                appThread.start();
            }
            appSync.notifyAll();
            while (appAnswered < ticket) {
                long wait = deadline - SystemClock.uptimeMillis();
                if (wait <= 0) {
                    return -1;
                }
                try {
                    appSync.wait(wait);
                } catch (InterruptedException ignored) {
                    // keep waiting until the deadline
                }
            }
        }
        return SystemClock.uptimeMillis();
    }

    /** The round-trip thread: makes one for the latest ticket whenever one is open. */
    private void appRoundTrips() {
        while (true) {
            long ticket;
            synchronized (appSync) {
                while (appAnswered >= appAsked) {
                    try {
                        appSync.wait();
                    } catch (InterruptedException ignored) {
                        // wait on
                    }
                }
                ticket = appAsked;
            }
            UiAutomation current = automation;
            if (current != null) {
                try {
                    // never answered from the cache: that would not reach the app
                    clearCache();
                    current.getRootInActiveWindow();
                } catch (Throwable ignored) {
                    // a failed round trip has nothing more to wait for
                }
            }
            synchronized (appSync) {
                appAnswered = ticket;
                appSync.notifyAll();
            }
        }
    }

    /**
     * Waits up to `maxMs` for window transitions and animations to end, with
     * the hidden UiAutomation.syncInputTransactions the platform runs around
     * every injected touch. Runs on its own thread, so the time limit holds
     * even when the system takes longer.
     */
    private void waitForTransitions(long maxMs) {
        if (maxMs <= 0) {
            return;
        }
        final Method sync = syncInputMethod();
        final UiAutomation current = automation;
        if (sync == null || current == null) {
            return;
        }
        final boolean takesFlag = syncInputTakesFlag;
        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    if (takesFlag) {
                        sync.invoke(current, true);
                    } else {
                        sync.invoke(current);
                    }
                } catch (Throwable ignored) {
                    // the event-based wait still applies
                }
            }
        }, "autonom-helper-transitions");
        worker.setDaemon(true);
        worker.start();
        try {
            worker.join(maxMs);
        } catch (InterruptedException ignored) {
            // go on with the event-based wait
        }
    }

    private synchronized Method syncInputMethod() {
        if (!syncInputLooked) {
            syncInputLooked = true;
            try {
                syncInput = UiAutomation.class.getDeclaredMethod("syncInputTransactions", boolean.class);
                syncInputTakesFlag = true;
            } catch (NoSuchMethodException e) {
                try {
                    // Android 11: always waits for animations
                    syncInput = UiAutomation.class.getDeclaredMethod("syncInputTransactions");
                    syncInputTakesFlag = false;
                } catch (NoSuchMethodException missing) {
                    syncInput = null;
                }
            }
            if (syncInput != null) {
                try {
                    syncInput.setAccessible(true);
                } catch (RuntimeException e) {
                    syncInput = null;
                }
            }
        }
        return syncInput;
    }

    // --- reads ------------------------------------------------------------------

    /** UiAutomation.clearCache() where the device has it (public on API 34+). */
    void clearCache() throws HelperError {
        UiAutomation current = ui();
        Method method = clearCacheMethod();
        if (method != null) {
            try {
                method.invoke(current);
            } catch (Throwable ignored) {
                // a stale cache only costs freshness; the read goes on
            }
        }
    }

    /** Looked up once; the request thread and the round-trip thread both clear. */
    private synchronized Method clearCacheMethod() {
        if (!clearCacheLooked) {
            clearCacheLooked = true;
            try {
                clearCache = UiAutomation.class.getMethod("clearCache");
            } catch (NoSuchMethodException e) {
                try {
                    clearCache = UiAutomation.class.getDeclaredMethod("clearCache");
                    clearCache.setAccessible(true);
                } catch (NoSuchMethodException | RuntimeException missing) {
                    clearCache = null;
                }
            }
        }
        return clearCache;
    }

    /**
     * Turns window tracking off for the XML tree that follows, so node
     * visibility is what the app reports, as in `uiautomator dump`. Always
     * followed by {@link #resumeWindowTracking}.
     */
    void pauseWindowTracking() {
        synchronized (events) {
            int slot = switchNext;
            switchNext = (switchNext + 1) % SWITCH_HISTORY;
            switchFrom[slot] = SystemClock.uptimeMillis();
            switchTo[slot] = Long.MAX_VALUE;
            switchOpen = slot;
        }
        setWindowTracking(false);
    }

    /** Turns window tracking back on; {@link #windows} waits for the windows. */
    void resumeWindowTracking() {
        setWindowTracking(true);
        closeSwitch();
    }

    private void closeSwitch() {
        synchronized (events) {
            if (switchOpen >= 0) {
                switchTo[switchOpen] = SystemClock.uptimeMillis();
            }
        }
    }

    /**
     * Sets or clears FLAG_RETRIEVE_INTERACTIVE_WINDOWS. Best effort: when the
     * device refuses, the read goes on with the flag as it was.
     */
    private void setWindowTracking(boolean on) {
        UiAutomation current = automation;
        if (current == null || windowTracking == on) {
            return;
        }
        try {
            AccessibilityServiceInfo info = current.getServiceInfo();
            if (info == null) {
                return;
            }
            if (on) {
                info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            } else {
                info.flags &= ~AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            }
            current.setServiceInfo(info); // also clears this process's node cache
            windowTracking = on;
        } catch (RuntimeException ignored) {
            // keep the current state
        }
    }

    /** The root of the active window, asked again for a short while when absent. */
    AccessibilityNodeInfo activeRoot() throws HelperError {
        UiAutomation current = ui();
        long deadline = SystemClock.uptimeMillis() + ROOT_RETRY_MS;
        while (true) {
            AccessibilityNodeInfo root = current.getRootInActiveWindow();
            if (root != null) {
                return root;
            }
            if (SystemClock.uptimeMillis() >= deadline) {
                throw new HelperError("no_active_window", "The device reports no active window");
            }
            SystemClock.sleep(100);
            clearCache();
        }
    }

    /**
     * Every window, with tracking on (the system waits until it has them
     * after tracking was switched back on).
     */
    List<AccessibilityWindowInfo> windows() throws HelperError {
        setWindowTracking(true);
        try {
            return ui().getWindows();
        } finally {
            // the windows-changed events of switching tracking back on are
            // dated before this point
            closeSwitch();
            synchronized (events) {
                switchOpen = -1;
            }
        }
    }

    /** The node that has input focus, read fresh. */
    AccessibilityNodeInfo focusedInput() throws HelperError {
        setWindowTracking(true);
        clearCache();
        return ui().findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
    }

    boolean inject(InputEvent event) throws HelperError {
        return ui().injectInputEvent(event, true);
    }

    /** Display 0: hidden DisplayManagerGlobal, as the shell's uiautomator reads it. */
    @SuppressWarnings("deprecation")
    static DisplayInfo display() throws HelperError {
        try {
            Class<?> globalType = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Object global = globalType.getMethod("getInstance").invoke(null);
            try {
                Object info = globalType.getMethod("getDisplayInfo", int.class)
                        .invoke(global, Display.DEFAULT_DISPLAY);
                if (info != null) {
                    Class<?> infoType = info.getClass();
                    int width = infoType.getField("logicalWidth").getInt(info);
                    int height = infoType.getField("logicalHeight").getInt(info);
                    int rotation = infoType.getField("rotation").getInt(info);
                    if (width > 0 && height > 0) {
                        return new DisplayInfo(width, height, rotation);
                    }
                }
            } catch (ReflectiveOperationException ignored) {
                // fall through to the Display object
            }
            Display display = (Display) globalType.getMethod("getRealDisplay", int.class)
                    .invoke(global, Display.DEFAULT_DISPLAY);
            Point size = new Point();
            display.getRealSize(size);
            return new DisplayInfo(size.x, size.y, display.getRotation());
        } catch (Throwable t) {
            throw new HelperError("internal", "Cannot read the display: " + t);
        }
    }
}
