package com.thomaswcode.phonerelay;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.content.ClipData;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.AttributionSource;
import android.graphics.Rect;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.Base64;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.RandomAccessFile;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * The phone client's helper, run as the shell user through app_process:
 *
 *   CLASSPATH=/data/local/tmp/phone-relay-helper.jar app_process / com.thomaswcode.phonerelay.Helper <command> ...
 *
 * It reaches what `adb shell` commands cannot: a UI tree that leaves other accessibility
 * services bound, multi-pointer touches, the clipboard (and pasting through it), and audio.
 * Hidden framework APIs are found by reflection, so it builds against the public android.jar.
 * Arguments that carry text or JSON are base64, so no shell quoting touches them, or "@path"
 * for a file holding the text (too long for a command line).
 */
public final class Helper {
    static final String VERSION = "1";
    static final String SHELL_PACKAGE = "com.android.shell";

    private Helper() {
    }

    public static void main(String[] args) {
        int code = 0;
        try {
            if (args.length == 0) throw new IllegalArgumentException("usage: Helper <command> ...");
            Looper.prepareMainLooper();
            switch (args[0]) {
                case "version":
                    System.out.println(VERSION);
                    break;
                case "ui":
                    Ui.dump(System.out);
                    break;
                case "gesture":
                    Input.gesture(new JSONObject(text(args[1])));
                    break;
                case "clip-get": {
                    String text = Clip.get();
                    System.out.println(new JSONObject().put("text", text == null ? JSONObject.NULL : text));
                    break;
                }
                case "clip-set":
                    Clip.set(text(args[1]));
                    break;
                case "clip-clear":
                    Clip.clear();
                    break;
                case "paste":
                    Clip.paste(text(args[1]));
                    break;
                case "audio":
                    Audio.record(Double.parseDouble(args[1]), args[2], args[3]);
                    break;
                default:
                    throw new IllegalArgumentException("unknown command: " + args[0]);
            }
        } catch (Throwable t) {
            // One line the client can show: the cause's class and message.
            Throwable cause = t;
            while (cause.getCause() != null) cause = cause.getCause();
            System.out.println("PHONE_RELAY_HELPER_ERROR " + cause.getClass().getSimpleName() + ": " + cause.getMessage());
            code = 1;
        }
        System.out.flush();
        // UiAutomation and binder threads would keep the VM alive.
        System.exit(code);
    }

    /** An argument's text: base64, or "@path" for a UTF-8 file. */
    static String text(String arg) throws IOException {
        if (arg.startsWith("@")) {
            try (RandomAccessFile f = new RandomAccessFile(arg.substring(1), "r")) {
                byte[] data = new byte[(int) f.length()];
                f.readFully(data);
                return new String(data, StandardCharsets.UTF_8);
            }
        }
        return new String(Base64.decode(arg, Base64.DEFAULT), StandardCharsets.UTF_8);
    }

    // ---- framework access (hidden APIs, by reflection) ----

    private static Context systemContext;

    /**
     * The system context of a bare ActivityThread, as scrcpy builds it. Android 12+ also needs the
     * thread's ConfigurationController, which only a real app's start would set; without it the
     * context's resources throw a NullPointerException.
     */
    static synchronized Context systemContext() throws Exception {
        if (systemContext == null) {
            Class<?> cls = Class.forName("android.app.ActivityThread");
            Constructor<?> ctor = cls.getDeclaredConstructor();
            ctor.setAccessible(true);
            Object thread = ctor.newInstance();
            Field current = cls.getDeclaredField("sCurrentActivityThread");
            current.setAccessible(true);
            current.set(null, thread);
            try {
                Class<?> controller = Class.forName("android.app.ConfigurationController");
                Class<?> internal = Class.forName("android.app.ActivityThreadInternal");
                Constructor<?> c = controller.getDeclaredConstructor(internal);
                c.setAccessible(true);
                Field field = cls.getDeclaredField("mConfigurationController");
                field.setAccessible(true);
                field.set(thread, c.newInstance(thread));
            } catch (ClassNotFoundException | NoSuchMethodException | NoSuchFieldException e) {
                // before Android 12: no ConfigurationController to fill in
            }
            Method get = cls.getDeclaredMethod("getSystemContext");
            get.setAccessible(true);
            systemContext = (Context) get.invoke(thread);
        }
        return systemContext;
    }

    /** A context that speaks as the shell package, for services that check the caller's package. */
    static Context shellContext(final Looper looper) throws Exception {
        return new ContextWrapper(systemContext()) {
            @Override
            public String getPackageName() {
                return SHELL_PACKAGE;
            }

            @Override
            public String getOpPackageName() {
                return SHELL_PACKAGE;
            }

            @Override
            public Looper getMainLooper() {
                return looper != null ? looper : super.getMainLooper();
            }

            @Override
            public AttributionSource getAttributionSource() {
                return new AttributionSource.Builder(Process.myUid()).setPackageName(SHELL_PACKAGE).build();
            }

            @Override
            public Context getApplicationContext() {
                return this;
            }
        };
    }

    static Object service(String name, String stubClass) throws Exception {
        Method getService = Class.forName("android.os.ServiceManager").getMethod("getService", String.class);
        IBinder binder = (IBinder) getService.invoke(null, name);
        if (binder == null) throw new IllegalStateException("no " + name + " service");
        return Class.forName(stubClass).getMethod("asInterface", IBinder.class).invoke(null, binder);
    }

    /**
     * Calls the first method called `name` whose parameters can be filled: `fixed` values by type,
     * in order (a ClipData, then Strings), then the remaining Strings as null and ints as 0 (user 0,
     * the default device). Covers the signatures that grew across Android versions.
     */
    static Object callAny(Object target, String name, Object... fixed) throws Exception {
        Exception last = null;
        for (Method m : target.getClass().getMethods()) {
            if (!m.getName().equals(name)) continue;
            Class<?>[] types = m.getParameterTypes();
            Object[] values = new Object[types.length];
            int next = 0;
            boolean fits = true;
            for (int i = 0; i < types.length; i++) {
                if (next < fixed.length && fixed[next] != null && types[i].isInstance(fixed[next])) {
                    values[i] = fixed[next++];
                } else if (types[i] == String.class) {
                    values[i] = null;
                } else if (types[i] == int.class) {
                    values[i] = 0;
                } else if (types[i] == boolean.class) {
                    values[i] = false;
                } else {
                    fits = false;
                    break;
                }
            }
            if (!fits || next < fixed.length) continue;
            try {
                return m.invoke(target, values);
            } catch (Exception e) {
                last = e;
            }
        }
        if (last != null) throw last;
        throw new NoSuchMethodException(name);
    }

    // ---- UI tree ----

    static final class Ui {
        /**
         * A uiautomator-style XML dump of every window, through a UiAutomation connected with
         * FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES, so other accessibility services stay bound.
         */
        static void dump(PrintStream out) throws Exception {
            HandlerThread thread = new HandlerThread("PhoneRelayUi");
            thread.start();
            UiAutomation ua = connect(thread.getLooper());
            try {
                AccessibilityServiceInfo info = ua.getServiceInfo();
                info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                        | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                        | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
                ua.setServiceInfo(info);
                try {
                    ua.waitForIdle(150, 2000);
                } catch (java.util.concurrent.TimeoutException e) {
                    // an animating screen: dump what is there
                }
                StringBuilder xml = new StringBuilder("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">\n");
                List<AccessibilityWindowInfo> windows = ua.getWindows();
                int dumped = 0;
                for (int i = windows.size() - 1; i >= 0; i--) { // bottom window first, as drawn
                    AccessibilityNodeInfo root = windows.get(i).getRoot();
                    if (root != null) {
                        node(xml, root, 0, 1);
                        dumped++;
                    }
                }
                if (dumped == 0) {
                    AccessibilityNodeInfo root = ua.getRootInActiveWindow();
                    if (root != null) node(xml, root, 0, 1);
                }
                xml.append("</hierarchy>\n");
                out.print(xml);
            } finally {
                try {
                    UiAutomation.class.getMethod("disconnect").invoke(ua);
                } catch (Exception ignored) {
                    // the process exits next anyway
                }
                thread.quitSafely();
            }
        }

        static UiAutomation connect(Looper looper) throws Exception {
            Class<?> connectionType = Class.forName("android.app.IUiAutomationConnection");
            Object connection = Class.forName("android.app.UiAutomationConnection").getDeclaredConstructor().newInstance();
            UiAutomation ua;
            try {
                Constructor<UiAutomation> c = UiAutomation.class.getDeclaredConstructor(Looper.class, connectionType);
                c.setAccessible(true);
                ua = c.newInstance(looper, connection);
            } catch (NoSuchMethodException e) {
                Constructor<UiAutomation> c = UiAutomation.class.getDeclaredConstructor(Context.class, connectionType);
                c.setAccessible(true);
                ua = c.newInstance(shellContext(looper), connection);
            }
            Method connect = UiAutomation.class.getDeclaredMethod("connect", int.class);
            connect.setAccessible(true);
            connect.invoke(ua, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            return ua;
        }

        static void node(StringBuilder xml, AccessibilityNodeInfo n, int index, int depth) {
            Rect r = new Rect();
            n.getBoundsInScreen(r);
            pad(xml, depth).append("<node index=\"").append(index).append('"');
            attr(xml, "text", n.getText());
            attr(xml, "resource-id", n.getViewIdResourceName());
            attr(xml, "class", n.getClassName());
            attr(xml, "package", n.getPackageName());
            attr(xml, "content-desc", n.getContentDescription());
            attr(xml, "checkable", n.isCheckable());
            attr(xml, "checked", n.isChecked());
            attr(xml, "clickable", n.isClickable());
            attr(xml, "enabled", n.isEnabled());
            attr(xml, "focusable", n.isFocusable());
            attr(xml, "focused", n.isFocused());
            attr(xml, "scrollable", n.isScrollable());
            attr(xml, "long-clickable", n.isLongClickable());
            attr(xml, "password", n.isPassword());
            attr(xml, "selected", n.isSelected());
            xml.append(" bounds=\"[").append(r.left).append(',').append(r.top).append("][")
                    .append(r.right).append(',').append(r.bottom).append("]\"");
            int count = n.getChildCount();
            if (count == 0) {
                xml.append(" />\n");
                return;
            }
            xml.append(">\n");
            int childIndex = 0;
            for (int i = 0; i < count; i++) {
                AccessibilityNodeInfo child = n.getChild(i);
                if (child == null) continue;
                if (child.isVisibleToUser()) node(xml, child, childIndex++, depth + 1);
            }
            pad(xml, depth).append("</node>\n");
        }

        static StringBuilder pad(StringBuilder xml, int depth) {
            for (int i = 0; i < depth; i++) xml.append("  ");
            return xml;
        }

        static void attr(StringBuilder xml, String name, Object value) {
            xml.append(' ').append(name).append("=\"");
            String s = value == null ? "" : value.toString();
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '&': xml.append("&amp;"); break;
                    case '<': xml.append("&lt;"); break;
                    case '>': xml.append("&gt;"); break;
                    case '"': xml.append("&quot;"); break;
                    case '\n': xml.append("&#10;"); break;
                    case '\r': xml.append("&#13;"); break;
                    case '\t': xml.append("&#9;"); break;
                    default:
                        // Characters XML 1.0 cannot carry (other controls, lone surrogates' kin) become spaces.
                        if (c < 0x20 || c == 0xFFFE || c == 0xFFFF) xml.append(' ');
                        else xml.append(c);
                }
            }
            xml.append('"');
        }
    }

    // ---- input ----

    static final class Input {
        static Object manager;
        static Method inject;

        static void inject(InputEvent event) throws Exception {
            if (manager == null) {
                Class<?> cls;
                try {
                    cls = Class.forName("android.hardware.input.InputManagerGlobal"); // Android 14+
                } catch (ClassNotFoundException e) {
                    cls = Class.forName("android.hardware.input.InputManager");
                }
                manager = cls.getDeclaredMethod("getInstance").invoke(null);
                inject = cls.getMethod("injectInputEvent", InputEvent.class, int.class);
            }
            // 0 = INJECT_INPUT_EVENT_MODE_ASYNC
            if (!(Boolean) inject.invoke(manager, event, 0)) throw new IllegalStateException("input injection refused");
        }

        /**
         * Touches with several fingers at once: {"pointers": [[[x, y], [x, y], ...], ...], "ms": 400}.
         * Every finger goes down at its path's first point, moves along its path in step, and lifts
         * at the end. Paths are sampled evenly over ms, every 10 ms or so.
         */
        static void gesture(JSONObject spec) throws Exception {
            JSONArray pointers = spec.getJSONArray("pointers");
            int n = pointers.length();
            if (n < 1 || n > 10) throw new IllegalArgumentException("1 to 10 pointers");
            double[][][] paths = new double[n][][];
            for (int p = 0; p < n; p++) {
                JSONArray path = pointers.getJSONArray(p);
                if (path.length() < 1) throw new IllegalArgumentException("pointer " + p + " has no points");
                paths[p] = new double[path.length()][2];
                for (int k = 0; k < path.length(); k++) {
                    paths[p][k][0] = path.getJSONArray(k).getDouble(0);
                    paths[p][k][1] = path.getJSONArray(k).getDouble(1);
                }
            }
            long ms = Math.max(0, spec.optLong("ms", 300));
            int steps = (int) Math.max(1, Math.min(ms / 10, 2000));

            MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[n];
            MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
            for (int p = 0; p < n; p++) {
                props[p] = new MotionEvent.PointerProperties();
                props[p].id = p;
                props[p].toolType = MotionEvent.TOOL_TYPE_FINGER;
                coords[p] = new MotionEvent.PointerCoords();
                coords[p].pressure = 1;
                coords[p].size = 1;
            }
            long down = SystemClock.uptimeMillis();
            place(coords, paths, 0);
            send(down, MotionEvent.ACTION_DOWN, 1, props, coords);
            for (int p = 1; p < n; p++) {
                send(down, MotionEvent.ACTION_POINTER_DOWN | (p << MotionEvent.ACTION_POINTER_INDEX_SHIFT), p + 1, props, coords);
            }
            for (int s = 1; s <= steps; s++) {
                long due = down + ms * s / steps;
                long wait = due - SystemClock.uptimeMillis();
                if (wait > 0) SystemClock.sleep(wait);
                place(coords, paths, (double) s / steps);
                send(down, MotionEvent.ACTION_MOVE, n, props, coords);
            }
            for (int p = n - 1; p >= 1; p--) {
                send(down, MotionEvent.ACTION_POINTER_UP | (p << MotionEvent.ACTION_POINTER_INDEX_SHIFT), p + 1, props, coords);
            }
            send(down, MotionEvent.ACTION_UP, 1, props, coords);
        }

        /** Each pointer at fraction t of the way along its path (by points, evenly). */
        static void place(MotionEvent.PointerCoords[] coords, double[][][] paths, double t) {
            for (int p = 0; p < paths.length; p++) {
                double[][] path = paths[p];
                double pos = t * (path.length - 1);
                int k = (int) Math.min(Math.floor(pos), path.length - 1);
                double f = pos - k;
                double[] a = path[k];
                double[] b = path[Math.min(k + 1, path.length - 1)];
                coords[p].x = (float) (a[0] + (b[0] - a[0]) * f);
                coords[p].y = (float) (a[1] + (b[1] - a[1]) * f);
            }
        }

        static void send(long down, int action, int count, MotionEvent.PointerProperties[] props,
                         MotionEvent.PointerCoords[] coords) throws Exception {
            MotionEvent e = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, count, props, coords,
                    0, 0, 1f, 1f, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
            try {
                inject(e);
            } finally {
                e.recycle();
            }
        }

        static void key(int code) throws Exception {
            long now = SystemClock.uptimeMillis();
            inject(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0, 0, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0,
                    InputDevice.SOURCE_KEYBOARD));
            inject(new KeyEvent(now, now, KeyEvent.ACTION_UP, code, 0, 0, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0,
                    InputDevice.SOURCE_KEYBOARD));
        }
    }

    // ---- clipboard ----

    static final class Clip {
        static Object clipboard() throws Exception {
            return service("clipboard", "android.content.IClipboard$Stub");
        }

        /** The clipboard's text, or null if it is empty or holds no text. */
        static String get() throws Exception {
            ClipData clip = (ClipData) callAny(clipboard(), "getPrimaryClip", SHELL_PACKAGE);
            if (clip == null || clip.getItemCount() == 0) return null;
            ClipData.Item item = clip.getItemAt(0);
            // Plain text needs no context; a URI or intent clip is turned into text with one.
            CharSequence text = item.getText() != null ? item.getText() : item.coerceToText(systemContext());
            return text == null ? null : text.toString();
        }

        static void set(String text) throws Exception {
            callAny(clipboard(), "setPrimaryClip", ClipData.newPlainText("phone-relay", text), SHELL_PACKAGE);
        }

        /**
         * Types any text through the clipboard: put it there, press paste, then put back what was
         * there before (if it could be read), after a pause for the app to take the paste.
         */
        static void paste(String text) throws Exception {
            String previous = null;
            boolean readable = true;
            try {
                previous = get();
            } catch (Exception e) {
                readable = false;
            }
            set(text);
            Input.key(KeyEvent.KEYCODE_PASTE);
            SystemClock.sleep(500);
            if (readable) {
                if (previous != null) set(previous);
                else clear();
            }
        }

        static void clear() throws Exception {
            callAny(clipboard(), "clearPrimaryClip", SHELL_PACKAGE);
        }
    }

    // ---- audio ----

    static final class Audio {
        static final int RATE = 48000;

        /** Records `seconds` of the phone's audio output ("output") or microphone ("mic") to a WAV file. */
        static void record(double seconds, String source, String path) throws Exception {
            int audioSource;
            if (source.equals("output")) audioSource = MediaRecorder.AudioSource.REMOTE_SUBMIX;
            else if (source.equals("mic")) audioSource = MediaRecorder.AudioSource.MIC;
            else throw new IllegalArgumentException("source must be output or mic");
            AudioFormat format = new AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(RATE)
                    .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
                    .build();
            int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT);
            AudioRecord.Builder builder = new AudioRecord.Builder()
                    .setAudioSource(audioSource)
                    .setAudioFormat(format)
                    .setBufferSizeInBytes(Math.max(min, RATE * 4 / 10) * 2);
            if (Build.VERSION.SDK_INT >= 31) builder.setContext(shellContext(null));
            AudioRecord rec = builder.build();
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("AudioRecord did not initialise");
            long total = (long) (seconds * RATE) * 4;
            try (RandomAccessFile out = new RandomAccessFile(path, "rw")) {
                out.setLength(0);
                out.write(new byte[44]); // header, filled in at the end
                rec.startRecording();
                byte[] buf = new byte[RATE * 4 / 10];
                long written = 0;
                while (written < total) {
                    int n = rec.read(buf, 0, (int) Math.min(buf.length, total - written));
                    if (n < 0) throw new IOException("AudioRecord.read " + n);
                    out.write(buf, 0, n);
                    written += n;
                }
                rec.stop();
                out.seek(0);
                out.write(wavHeader(written));
            } finally {
                rec.release();
            }
        }

        static byte[] wavHeader(long dataBytes) {
            ByteArrayOutputStream h = new ByteArrayOutputStream(44);
            h.write('R'); h.write('I'); h.write('F'); h.write('F');
            le(h, 36 + dataBytes, 4);
            h.write('W'); h.write('A'); h.write('V'); h.write('E');
            h.write('f'); h.write('m'); h.write('t'); h.write(' ');
            le(h, 16, 4);
            le(h, 1, 2); // PCM
            le(h, 2, 2); // stereo
            le(h, RATE, 4);
            le(h, RATE * 4, 4); // bytes per second
            le(h, 4, 2); // block align
            le(h, 16, 2); // bits per sample
            h.write('d'); h.write('a'); h.write('t'); h.write('a');
            le(h, dataBytes, 4);
            return h.toByteArray();
        }

        static void le(OutputStream out, long v, int bytes) {
            try {
                for (int i = 0; i < bytes; i++) out.write((int) (v >> (8 * i)) & 0xFF);
            } catch (IOException e) {
                throw new IllegalStateException(e);
            }
        }
    }
}
