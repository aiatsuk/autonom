package dev.autonom.helper;

import android.graphics.Rect;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.xmlpull.v1.XmlSerializer;

import java.io.IOException;
import java.io.StringWriter;
import java.util.List;

/**
 * The screen as the XML `uiautomator dump` writes: the same root (the active
 * window), the same node attributes in the same order, the same character
 * clean-up and the same serializer, so a host parser cannot tell the two
 * apart. The list of every window is reported separately.
 */
final class TreeXml {
    /** Layout classes uiautomator never marks "not accessibility friendly". */
    private static final String[] NAF_EXCLUDED = {
        "android.widget.GridView", "android.widget.GridLayout",
        "android.widget.ListView", "android.widget.TableLayout",
    };
    /** A guard against runaway trees; real screens are far shallower. */
    private static final int MAX_DEPTH = 1000;

    private TreeXml() {
    }

    static String hierarchy(AccessibilityNodeInfo root, Automation.DisplayInfo display)
            throws IOException {
        XmlSerializer xml = Xml.newSerializer();
        StringWriter out = new StringWriter();
        xml.setOutput(out);
        xml.startDocument("UTF-8", true);
        xml.startTag("", "hierarchy");
        xml.attribute("", "rotation", Integer.toString(display.rotation));
        node(xml, root, 0, display.width, display.height, 0);
        xml.endTag("", "hierarchy");
        xml.endDocument();
        return out.toString();
    }

    @SuppressWarnings("deprecation")
    private static void node(XmlSerializer xml, AccessibilityNodeInfo node, int index,
            int width, int height, int depth) throws IOException {
        xml.startTag("", "node");
        if (!nafExcluded(node) && !nafAccepted(node)) {
            xml.attribute("", "NAF", "true");
        }
        xml.attribute("", "index", Integer.toString(index));
        xml.attribute("", "text", clean(node.getText()));
        xml.attribute("", "resource-id", clean(node.getViewIdResourceName()));
        xml.attribute("", "class", clean(node.getClassName()));
        xml.attribute("", "package", clean(node.getPackageName()));
        xml.attribute("", "content-desc", clean(node.getContentDescription()));
        xml.attribute("", "checkable", Boolean.toString(node.isCheckable()));
        xml.attribute("", "checked", Boolean.toString(node.isChecked()));
        xml.attribute("", "clickable", Boolean.toString(node.isClickable()));
        xml.attribute("", "enabled", Boolean.toString(node.isEnabled()));
        xml.attribute("", "focusable", Boolean.toString(node.isFocusable()));
        xml.attribute("", "focused", Boolean.toString(node.isFocused()));
        xml.attribute("", "scrollable", Boolean.toString(node.isScrollable()));
        xml.attribute("", "long-clickable", Boolean.toString(node.isLongClickable()));
        xml.attribute("", "password", Boolean.toString(node.isPassword()));
        xml.attribute("", "selected", Boolean.toString(node.isSelected()));
        xml.attribute("", "bounds", visibleBounds(node, width, height).toShortString());
        xml.attribute("", "drawing-order", Integer.toString(node.getDrawingOrder()));
        xml.attribute("", "hint", clean(node.getHintText()));
        if (depth < MAX_DEPTH) {
            int count = node.getChildCount();
            for (int i = 0; i < count; i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                // uiautomator skips missing and invisible children but keeps
                // the child's position as its index.
                if (child != null && child.isVisibleToUser()) {
                    node(xml, child, i, width, height, depth + 1);
                }
            }
        }
        xml.endTag("", "node");
    }

    /** Bounds clipped to the display; left as they are when off screen. */
    static Rect visibleBounds(AccessibilityNodeInfo node, int width, int height) {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        bounds.intersect(new Rect(0, 0, width, height));
        return bounds;
    }

    private static boolean nafExcluded(AccessibilityNodeInfo node) {
        String className = clean(node.getClassName());
        for (String excluded : NAF_EXCLUDED) {
            if (className.endsWith(excluded)) {
                return true;
            }
        }
        return false;
    }

    /**
     * False for an enabled, clickable node with no text, no description and
     * no labelled descendant: uiautomator marks those NAF="true".
     */
    private static boolean nafAccepted(AccessibilityNodeInfo node) {
        boolean unlabelled = node.isClickable() && node.isEnabled()
                && clean(node.getContentDescription()).isEmpty()
                && clean(node.getText()).isEmpty();
        return !unlabelled || labelledDescendant(node, 0);
    }

    private static boolean labelledDescendant(AccessibilityNodeInfo node, int depth) {
        if (depth >= MAX_DEPTH) {
            return false;
        }
        int count = node.getChildCount();
        for (int i = 0; i < count; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) {
                continue;
            }
            if (!clean(child.getContentDescription()).isEmpty() || !clean(child.getText()).isEmpty()) {
                return true;
            }
            if (labelledDescendant(child, depth + 1)) {
                return true;
            }
        }
        return false;
    }

    /**
     * Text as uiautomator writes it: null becomes "", and the control and
     * non-character code units it strips become ".". Code units the
     * serializer would refuse (NUL, lone surrogates, U+FFFE, U+FFFF) become
     * "." too, where uiautomator would fail the whole dump.
     */
    static String clean(CharSequence value) {
        if (value == null) {
            return "";
        }
        int length = value.length();
        StringBuilder out = new StringBuilder(length);
        for (int i = 0; i < length; i++) {
            char c = value.charAt(i);
            if (strippedByUiautomator(c)) {
                out.append('.');
            } else if (Character.isHighSurrogate(c) && i + 1 < length
                    && Character.isLowSurrogate(value.charAt(i + 1))) {
                out.append(c).append(value.charAt(i + 1));
                i++;
            } else if (Character.isSurrogate(c) || c == 0 || c == 0xFFFE || c == 0xFFFF) {
                out.append('.');
            } else {
                out.append(c);
            }
        }
        return out.toString();
    }

    private static boolean strippedByUiautomator(char c) {
        return (c >= 0x1 && c <= 0x8) || (c >= 0xB && c <= 0xC) || (c >= 0xE && c <= 0x1F)
                || (c >= 0x7F && c <= 0x84) || (c >= 0x86 && c <= 0x9F)
                || (c >= 0xFDD0 && c <= 0xFDDF);
    }

    static String windowType(int type) {
        switch (type) {
            case AccessibilityWindowInfo.TYPE_APPLICATION:
                return "application";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD:
                return "input_method";
            case AccessibilityWindowInfo.TYPE_SYSTEM:
                return "system";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY:
                return "accessibility_overlay";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER:
                return "split_screen_divider";
            case 6: // TYPE_MAGNIFICATION_OVERLAY (API 33)
                return "magnification_overlay";
            default:
                return "unknown";
        }
    }

    /** Every window the device reports, topmost layer first as it lists them. */
    static JSONArray windows(List<AccessibilityWindowInfo> windows) throws JSONException {
        JSONArray list = new JSONArray();
        if (windows == null) {
            return list;
        }
        for (AccessibilityWindowInfo window : windows) {
            if (window == null) {
                continue;
            }
            Rect bounds = new Rect();
            window.getBoundsInScreen(bounds);
            AccessibilityNodeInfo root = window.getRoot();
            CharSequence title = window.getTitle();
            CharSequence pkg = root == null ? null : root.getPackageName();
            JSONObject item = new JSONObject();
            item.put("id", window.getId());
            item.put("type", windowType(window.getType()));
            item.put("type_id", window.getType());
            item.put("layer", window.getLayer());
            item.put("active", window.isActive());
            item.put("focused", window.isFocused());
            item.put("title", title == null ? JSONObject.NULL : clean(title));
            item.put("package", pkg == null ? JSONObject.NULL : clean(pkg));
            item.put("bounds", rect(bounds));
            list.put(item);
        }
        return list;
    }

    static JSONArray rect(Rect bounds) {
        JSONArray value = new JSONArray();
        value.put(bounds.left);
        value.put(bounds.top);
        value.put(bounds.right);
        value.put(bounds.bottom);
        return value;
    }
}
