package dev.autonom.helper;

import android.graphics.Rect;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.ArrayList;
import java.util.List;

/**
 * Input injected through the UiAutomation connection: touches as
 * MotionEvents, keys as KeyEvents, and text as key events where the virtual
 * keyboard map can produce a character and as text inserted at the cursor
 * where it cannot. None of them repeats an event. Touches and keys either
 * complete or throw a HelperError; typing sends its text in parts and stops
 * at the first part that does not show up in the field, and says so.
 */
final class Input {
    /** A typed or set part may take this long to show up in the field ... */
    private static final long TEXT_WAIT_BASE_MS = 1500;
    /** ... plus this much per character of the part ... */
    private static final long TEXT_WAIT_PER_CHAR_MS = 60;
    /** ... and never longer than this. */
    private static final long TEXT_WAIT_MAX_MS = 5000;
    /**
     * All the waits of one `type` end this long after it started, so its
     * reply comes in a known time on a slow device; the parts not sent by
     * then are not sent at all.
     */
    static final long TYPE_WAIT_TOTAL_MS = 6000;
    /** Pause between two reads of the field while waiting for text. */
    private static final long TEXT_POLL_MS = 25;
    /**
     * Before key events that follow a set-text: no accessibility event for
     * this long after the set-text, at most {@link #AFTER_SET_TEXT_MAX_MS}.
     * An app may restart the keyboard's input connection when its text
     * changes (a search field going from empty to not empty does), and a key
     * the keyboard handles itself, such as a space, sent while that restart
     * runs is committed to the old connection and lost.
     */
    private static final long AFTER_SET_TEXT_QUIET_MS = 200;
    private static final long AFTER_SET_TEXT_MAX_MS = 1000;
    /** Pause between the move events of a swipe (about 120 per second). */
    private static final long MOVE_INTERVAL_MS = 8;

    private final Automation automation;
    private KeyCharacterMap keyMap;
    private int touchDevice = Integer.MIN_VALUE;

    Input(Automation automation) {
        this.automation = automation;
    }

    // --- touches ------------------------------------------------------------

    void tap(float x, float y) throws HelperError {
        long now = SystemClock.uptimeMillis();
        touch(now, now, MotionEvent.ACTION_DOWN, x, y, 1f, "down");
        touch(now, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y, 0f, "up");
    }

    void longPress(float x, float y, int durationMs) throws HelperError {
        long down = SystemClock.uptimeMillis();
        touch(down, down, MotionEvent.ACTION_DOWN, x, y, 1f, "down");
        SystemClock.sleep(durationMs);
        touch(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y, 0f, "up");
    }

    void swipe(float x1, float y1, float x2, float y2, int durationMs) throws HelperError {
        long down = SystemClock.uptimeMillis();
        long end = down + durationMs;
        touch(down, down, MotionEvent.ACTION_DOWN, x1, y1, 1f, "down");
        long now = SystemClock.uptimeMillis();
        while (now < end) {
            float alpha = (now - down) / (float) durationMs;
            touch(down, now, MotionEvent.ACTION_MOVE, lerp(x1, x2, alpha), lerp(y1, y2, alpha), 1f, "move");
            SystemClock.sleep(MOVE_INTERVAL_MS);
            now = SystemClock.uptimeMillis();
        }
        touch(down, now, MotionEvent.ACTION_MOVE, x2, y2, 1f, "move");
        touch(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x2, y2, 0f, "up");
    }

    private static float lerp(float from, float to, float alpha) {
        return from + (to - from) * alpha;
    }

    private void touch(long downTime, long eventTime, int action, float x, float y, float pressure,
            String what) throws HelperError {
        MotionEvent.PointerProperties[] properties = {new MotionEvent.PointerProperties()};
        properties[0].id = 0;
        properties[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = x;
        coords[0].y = y;
        coords[0].pressure = pressure;
        coords[0].size = 1f;
        MotionEvent event = MotionEvent.obtain(downTime, eventTime, action, 1, properties, coords,
                0, 0, 1f, 1f, touchDevice(), 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            if (!automation.inject(event)) {
                throw new HelperError("inject_failed", "The device refused the touch " + what + " event");
            }
        } finally {
            event.recycle();
        }
    }

    /** The first touchscreen's id, as `input` picks one; 0 when none is listed. */
    private int touchDevice() {
        if (touchDevice == Integer.MIN_VALUE) {
            touchDevice = 0;
            try {
                for (int id : InputDevice.getDeviceIds()) {
                    InputDevice device = InputDevice.getDevice(id);
                    if (device != null && device.supportsSource(InputDevice.SOURCE_TOUCHSCREEN)) {
                        touchDevice = id;
                        break;
                    }
                }
            } catch (RuntimeException ignored) {
                // keep 0
            }
        }
        return touchDevice;
    }

    // --- keys ---------------------------------------------------------------

    void key(int keyCode) throws HelperError {
        long now = SystemClock.uptimeMillis();
        injectKey(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, keyCode, 0, 0,
                KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
        long up = SystemClock.uptimeMillis();
        injectKey(new KeyEvent(now, up, KeyEvent.ACTION_UP, keyCode, 0, 0,
                KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
    }

    private void injectKey(KeyEvent event) throws HelperError {
        if (!automation.inject(event)) {
            throw new HelperError("inject_failed", "The device refused a key event");
        }
    }

    private KeyCharacterMap keyMap() {
        if (keyMap == null) {
            keyMap = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD);
        }
        return keyMap;
    }

    // --- text ---------------------------------------------------------------

    /** A run of text typed one way: key events (`events` set) or set-text. */
    private static final class Segment {
        final String text;
        final KeyEvent[] events;

        Segment(String text, KeyEvent[] events) {
            this.text = text;
            this.events = events;
        }
    }

    /**
     * The outcome of `type` or `set_text`: the method, what the field shows
     * after it (null when that cannot be read: a password field, or a field
     * that went away or exposes no text), and whether that is the expected
     * text (true), a part did not show up in time so typing stopped (false),
     * or the text cannot be read to tell (null).
     */
    static final class Typed {
        final String method;
        final String textAfter;
        final Boolean complete;

        Typed(String method, String textAfter, Boolean complete) {
            this.method = method;
            this.textAfter = textAfter;
            this.complete = complete;
        }
    }

    /** The focused editable field, or the reason there is none. */
    AccessibilityNodeInfo editableFocus() throws HelperError {
        AccessibilityNodeInfo field = automation.focusedInput();
        if (field == null) {
            throw new HelperError("no_focus", "No view has input focus; tap a text field first");
        }
        if (!field.isEditable()) {
            throw new HelperError("not_editable", "The focused view is not an editable text field");
        }
        return field;
    }

    /**
     * Types `text` at the cursor of the focused field, part by part: a run of
     * characters the key map produces as key events, the rest with set-text
     * of the whole expected text, the cursor put back behind the insert.
     * Everything is decided before the first event, so a refusal never
     * leaves half the text typed. Each part must show up in the field before
     * the next one is sent; when one has not shown up by the end of its wait,
     * nothing more is sent and the reply says what the field shows.
     */
    Typed type(String text) throws HelperError {
        long started = SystemClock.uptimeMillis();
        AccessibilityNodeInfo field = editableFocus();
        List<Segment> segments = segments(text);
        boolean keys = false;
        boolean setText = false;
        for (Segment segment : segments) {
            if (segment.events != null) {
                keys = true;
            } else {
                setText = true;
            }
        }
        String method = !setText ? "keys" : keys ? "mixed" : "set_text";
        if (!field.refresh()) {
            throw new HelperError("no_focus", "The focused field went away before typing");
        }
        boolean[] sent = {false};
        if (unreadable(field)) {
            // A password field does not show its text: nothing can be checked.
            if (!setText) {
                for (Segment segment : segments) {
                    HelperError failed = sendKeys(segment.events, sent);
                    if (failed != null) {
                        if (!sent[0]) {
                            throw failed;
                        }
                        return new Typed(method, null, Boolean.FALSE);
                    }
                }
                return new Typed(method, null, null);
            }
            // Text cannot be inserted into it without the risk of overwriting
            // what is there. An empty one can take the whole text in one set-text.
            if (!readableEmpty(field)) {
                throw new HelperError("text_not_typable",
                        "The password field may already hold text, which cannot be read, and "
                                + "this text needs set-text; clear the field or type only "
                                + "characters the keyboard map can produce");
            }
            performSetText(field, text);
            return new Typed("set_text", null, null);
        }
        String start = currentText(field);
        int[] selection = selection(field, start);
        String before = start.substring(0, selection[0]);
        String after = start.substring(selection[1]);
        long allEnd = started + TYPE_WAIT_TOTAL_MS;
        long setTextAt = -1; // when the previous part's set-text was sent
        StringBuilder typed = new StringBuilder();
        for (int i = 0; i < segments.size(); i++) {
            Segment segment = segments.get(i);
            if (i > 0 && SystemClock.uptimeMillis() >= allEnd) {
                // out of time: the rest is not sent
                return new Typed(method, readText(field), Boolean.FALSE);
            }
            typed.append(segment.text);
            String expected = before + typed + after;
            if (segment.events != null && setTextAt >= 0) {
                // let the app and the keyboard take in the set-text first
                automation.quietSince(setTextAt, AFTER_SET_TEXT_QUIET_MS,
                        Math.min(AFTER_SET_TEXT_MAX_MS, allEnd - SystemClock.uptimeMillis()));
            }
            setTextAt = segment.events != null ? -1 : SystemClock.uptimeMillis();
            HelperError failed = segment.events != null
                    ? sendKeys(segment.events, sent)
                    : sendSetText(field, expected, before.length() + typed.length(), sent);
            if (failed != null) {
                if (!sent[0]) {
                    throw failed; // nothing reached the field: a plain refusal
                }
                // Part of the text is in: an error now would read as "not typed".
                return new Typed(method, readText(field), Boolean.FALSE);
            }
            long deadline = Math.min(SystemClock.uptimeMillis() + textWaitMs(segment.text), allEnd);
            if (!waitForText(field, expected, deadline)) {
                String shown = readText(field);
                if (!expected.equals(shown)) {
                    // Never go on from text that is not what this part should
                    // have left: the next set-text would be computed from it and
                    // lose characters. Report what the field shows instead.
                    boolean last = i == segments.size() - 1;
                    return new Typed(method, shown, last && shown == null ? null : Boolean.FALSE);
                }
            }
        }
        return new Typed(method, before + typed + after, Boolean.TRUE);
    }

    /**
     * A password field: its node carries the masked text, not the text. Any
     * other field is readable; one whose node has no text is empty (an empty
     * TextView without a hint reports no text) and is typed into as empty.
     */
    private static boolean unreadable(AccessibilityNodeInfo field) {
        return field.isPassword();
    }

    /**
     * Injects the key events of one part; null when the device took them
     * all, else the failure. `sent[0]` turns true once any event was taken.
     */
    private HelperError sendKeys(KeyEvent[] events, boolean[] sent) {
        for (KeyEvent event : events) {
            try {
                injectKey(event);
            } catch (HelperError e) {
                return e;
            } catch (RuntimeException e) {
                return new HelperError("internal", "A key event failed: " + e);
            }
            sent[0] = true;
        }
        return null;
    }

    /**
     * Sets the whole `text` and puts the cursor at `cursor`; null when the
     * field took it, else the failure. `sent[0]` turns true once it did.
     */
    private static HelperError sendSetText(AccessibilityNodeInfo field, String text, int cursor,
            boolean[] sent) {
        try {
            performSetText(field, text);
        } catch (HelperError e) {
            return e;
        } catch (RuntimeException e) {
            return new HelperError("internal", "set-text failed: " + e);
        }
        sent[0] = true;
        if (cursor != text.length()) {
            // set-text leaves the cursor at the end; move it back behind the
            // inserted text (only when needed: selecting starts a selection mode).
            // A refused move shows up as the next part not landing.
            Bundle arguments = new Bundle();
            arguments.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, cursor);
            arguments.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, cursor);
            try {
                field.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, arguments);
            } catch (RuntimeException ignored) {
                // same as refused
            }
        }
        return null;
    }

    /** How long a part of `text` may take to show up. */
    static long textWaitMs(String text) {
        long chars = text.codePointCount(0, text.length());
        return Math.min(TEXT_WAIT_MAX_MS, TEXT_WAIT_BASE_MS + TEXT_WAIT_PER_CHAR_MS * chars);
    }

    private List<Segment> segments(String text) {
        List<Segment> segments = new ArrayList<>();
        StringBuilder run = new StringBuilder();
        Boolean runIsKeys = null;
        int i = 0;
        while (i < text.length()) {
            int codePoint = text.codePointAt(i);
            int width = Character.charCount(codePoint);
            String unit = text.substring(i, i + width);
            boolean byKeys = width == 1 && mappable(unit.charAt(0));
            if (runIsKeys != null && runIsKeys != byKeys) {
                segments.add(segment(run.toString(), runIsKeys));
                run.setLength(0);
            }
            runIsKeys = byKeys;
            run.append(unit);
            i += width;
        }
        if (run.length() > 0) {
            segments.add(segment(run.toString(), runIsKeys));
        }
        return segments;
    }

    private boolean mappable(char c) {
        KeyEvent[] events = keyMap().getEvents(new char[] {c});
        return events != null && events.length > 0;
    }

    private Segment segment(String text, boolean byKeys) {
        if (!byKeys) {
            return new Segment(text, null);
        }
        KeyEvent[] events = keyMap().getEvents(text.toCharArray());
        return events == null ? new Segment(text, null) : new Segment(text, events);
    }

    /** Replaces the whole text of the field and says what it shows after. */
    Typed replace(AccessibilityNodeInfo field, String text) throws HelperError {
        boolean unreadable = unreadable(field);
        performSetText(field, text);
        if (unreadable) {
            return new Typed("set_text", null, null);
        }
        if (waitForText(field, text, SystemClock.uptimeMillis() + textWaitMs(text))) {
            return new Typed("set_text", text, Boolean.TRUE);
        }
        String shown = readText(field);
        if (text.equals(shown)) { // came in right after the wait
            return new Typed("set_text", text, Boolean.TRUE);
        }
        return new Typed("set_text", shown, shown == null ? null : Boolean.FALSE);
    }

    private static void performSetText(AccessibilityNodeInfo field, String text) throws HelperError {
        Bundle arguments = new Bundle();
        arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
            throw new HelperError("inject_failed", "The field refused set-text");
        }
    }

    /**
     * True once the field shows `expected`; false at `deadline` (at least one
     * look) or as soon as the field is gone.
     */
    private static boolean waitForText(AccessibilityNodeInfo field, String expected, long deadline) {
        while (true) {
            if (!field.refresh()) {
                return false;
            }
            if (expected.equals(currentText(field))) {
                return true;
            }
            if (SystemClock.uptimeMillis() >= deadline) {
                return false;
            }
            SystemClock.sleep(TEXT_POLL_MS);
        }
    }

    /**
     * The field's text read fresh: "" while it shows its hint; null when it
     * cannot be read (gone, a password field, or no text exposed).
     */
    private static String readText(AccessibilityNodeInfo field) {
        if (!field.refresh() || field.isPassword()) {
            return null;
        }
        if (field.isShowingHintText()) {
            return "";
        }
        CharSequence text = field.getText();
        return text == null ? null : text.toString();
    }

    /** The field's text as cached; "" while it shows its hint or has none. */
    static String currentText(AccessibilityNodeInfo field) {
        if (field.isShowingHintText()) {
            return "";
        }
        CharSequence text = field.getText();
        return text == null ? "" : text.toString();
    }

    /** True when the field is known to be empty (not merely unreadable). */
    private static boolean readableEmpty(AccessibilityNodeInfo field) {
        field.refresh();
        if (field.isShowingHintText()) {
            return true;
        }
        CharSequence text = field.getText();
        return text != null && text.length() == 0;
    }

    /** [start, end] of the selection within `text`; the end when unknown. */
    private static int[] selection(AccessibilityNodeInfo field, String text) {
        int start = field.getTextSelectionStart();
        int end = field.getTextSelectionEnd();
        if (start < 0 || end < 0 || start > text.length() || end > text.length()) {
            return new int[] {text.length(), text.length()};
        }
        return start <= end ? new int[] {start, end} : new int[] {end, start};
    }

    static boolean boundsMatch(Rect bounds, int[] expected) {
        return bounds.left == expected[0] && bounds.top == expected[1]
                && bounds.right == expected[2] && bounds.bottom == expected[3];
    }
}
