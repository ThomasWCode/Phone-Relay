package com.thomaswcode.phonerelay

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Path
import android.graphics.Rect
import android.media.AudioManager
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.BatteryManager
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.Display
import android.view.KeyEvent
import android.view.WindowManager
import android.view.accessibility.AccessibilityNodeInfo
import com.thomaswcode.phonerelay.commands.ApkInfo
import com.thomaswcode.phonerelay.commands.CommandError
import com.thomaswcode.phonerelay.commands.Device
import com.thomaswcode.phonerelay.commands.DevToolsLink
import com.thomaswcode.phonerelay.commands.Field
import com.thomaswcode.phonerelay.commands.FileEntry
import com.thomaswcode.phonerelay.commands.Frame
import com.thomaswcode.phonerelay.commands.GlobalAction
import com.thomaswcode.phonerelay.commands.IntentSpec
import com.thomaswcode.phonerelay.commands.PackageDetails
import com.thomaswcode.phonerelay.commands.Paths
import com.thomaswcode.phonerelay.commands.Point
import com.thomaswcode.phonerelay.commands.Rotation
import com.thomaswcode.phonerelay.commands.Shot
import com.thomaswcode.phonerelay.commands.Size
import com.thomaswcode.phonerelay.commands.UiNode
import com.thomaswcode.phonerelay.commands.extraValue
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileNotFoundException
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject

/** The Device the commands drive, done with the accessibility service and Android's own APIs. */
class AndroidDevice(
    private val context: Context,
    private val capture: Capture,
    private val adb: AdbShell,
    private val version: String,
) : Device {

    private val service: PhoneAccessibilityService
        get() = PhoneAccessibilityService.instance
            ?: throw CommandError("permission_missing", "the Phone Relay accessibility service is off: Settings > Accessibility > Installed apps > Phone Relay")

    private fun <T> missing(what: String): T = throw CommandError("permission_missing", what)

    override fun foreground(): String? =
        PhoneAccessibilityService.instance?.rootInActiveWindow?.packageName?.toString()

    override fun awake(): Boolean = context.getSystemService(PowerManager::class.java).isInteractive

    override fun battery(): Int? = context.getSystemService(BatteryManager::class.java)
        .getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).takeIf { it in 0..100 }

    override fun screenSize(): com.thomaswcode.phonerelay.commands.Size {
        val b = context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds
        return Size(b.width(), b.height())
    }

    override fun rotation(): Rotation {
        val cr = context.contentResolver
        return Rotation(
            auto = runCatching { Settings.System.getInt(cr, Settings.System.ACCELEROMETER_ROTATION) == 1 }.getOrNull(),
            userRotation = runCatching { Settings.System.getInt(cr, Settings.System.USER_ROTATION) }.getOrNull(),
        )
    }

    override fun setRotation(rotation: Int?) {
        if (!Settings.System.canWrite(context)) missing<Unit>("allow Modify system settings for Phone Relay (the app's own screen has the button)")
        val cr = context.contentResolver
        if (rotation == null) {
            Settings.System.putInt(cr, Settings.System.ACCELEROMETER_ROTATION, 1)
        } else {
            Settings.System.putInt(cr, Settings.System.ACCELEROMETER_ROTATION, 0)
            Settings.System.putInt(cr, Settings.System.USER_ROTATION, rotation)
        }
    }

    // ---- screenshots ----

    private suspend fun capture(): Bitmap {
        val s = service
        repeat(5) { attempt ->
            val result = runCatching { captureOnce(s) }
            result.getOrNull()?.let { return it }
            val e = result.exceptionOrNull()
            // Android allows about three screenshots a second; wait and try again.
            if (e is CommandError && e.detail.contains("interval") && attempt < 4) delay(350) else throw e!!
        }
        throw CommandError("device_error", "no screenshot")
    }

    private suspend fun captureOnce(s: AccessibilityService): Bitmap = suspendCancellableCoroutine { cont ->
        s.takeScreenshot(Display.DEFAULT_DISPLAY, context.mainExecutor, object : AccessibilityService.TakeScreenshotCallback {
            override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                val buffer = result.hardwareBuffer
                val bitmap = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace)?.copy(Bitmap.Config.ARGB_8888, false)
                buffer.close()
                if (bitmap == null) cont.resumeWithException(CommandError("device_error", "screenshot could not be read"))
                else cont.resume(bitmap)
            }

            override fun onFailure(errorCode: Int) {
                val why = when (errorCode) {
                    AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT -> "interval too short"
                    AccessibilityService.ERROR_TAKE_SCREENSHOT_SECURE_WINDOW -> "a secure window (FLAG_SECURE) is on the screen"
                    AccessibilityService.ERROR_TAKE_SCREENSHOT_NO_ACCESSIBILITY_ACCESS -> "no screenshot permission"
                    else -> "error $errorCode"
                }
                cont.resumeWithException(CommandError("device_error", "screenshot failed: $why"))
            }
        })
    }

    override suspend fun screenshot(scale: Double): Shot {
        val full = capture()
        val w = maxOf(1, Math.round(full.width * scale).toInt())
        val h = maxOf(1, Math.round(full.height * scale).toInt())
        val scaled = if (scale < 1) Bitmap.createScaledBitmap(full, w, h, true) else full
        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, 60, out)
        return Shot(out.toByteArray(), scaled.width, scaled.height, full.width, full.height, scale)
    }

    override suspend fun frame(): Frame {
        val full = capture()
        val small = Bitmap.createScaledBitmap(full, maxOf(1, full.width / 8), maxOf(1, full.height / 8), true)
        val pixels = IntArray(small.width * small.height)
        small.getPixels(pixels, 0, small.width, 0, 0, small.width, small.height)
        val grey = ByteArray(pixels.size) { i ->
            val p = pixels[i]
            ((Color.red(p) * 30 + Color.green(p) * 59 + Color.blue(p) * 11) / 100).toByte()
        }
        return Frame(small.width, small.height, grey)
    }

    // ---- the UI tree ----

    /** A node of the UI tree; Capture presses the capture prompt's button through it. */
    class Node(val n: AccessibilityNodeInfo) : UiNode {
        override val text get() = n.text?.toString()
        override val desc get() = n.contentDescription?.toString()
        override val viewId: String? get() = n.viewIdResourceName
        override val className get() = n.className?.toString()
        override val pkg get() = n.packageName?.toString()
        override val bounds: IntArray get() = Rect().also { n.getBoundsInScreen(it) }.let { intArrayOf(it.left, it.top, it.right, it.bottom) }
        override val clickable get() = n.isClickable
        override val scrollable get() = n.isScrollable
        override val editable get() = n.isEditable
        override val focused get() = n.isFocused
        override val password get() = n.isPassword
        @Suppress("DEPRECATION")
        override val checked get() = n.isChecked
        override val visible get() = n.isVisibleToUser
        override val children: List<UiNode> get() = (0 until n.childCount).mapNotNull { n.getChild(it) }.map(::Node)
    }

    override fun uiRoots(): List<UiNode> {
        val s = service
        val roots = s.windows.asReversed().mapNotNull { it.root }.map(::Node)
        return roots.ifEmpty { listOfNotNull(s.rootInActiveWindow?.let(::Node)) }
    }

    // ---- touch ----

    override suspend fun gesture(paths: List<List<Point>>, ms: Long) {
        val s = service
        if (paths.size > GestureDescription.getMaxStrokeCount()) {
            throw CommandError("unsupported", "Android takes at most ${GestureDescription.getMaxStrokeCount()} fingers at once")
        }
        if (paths.any { path -> path.any { it.x < 0 || it.y < 0 } }) {
            throw CommandError("invalid_args", "Android takes no point beyond the screen's top or left edge")
        }
        // Android takes a minute at most in one gesture: a longer one goes as slices, each stroke continuing the last, the fingers staying down.
        val slices = Paths.slices(ms, GestureDescription.getMaxGestureDuration())
        var strokes: List<GestureDescription.StrokeDescription>? = null
        for (i in 0 until slices) {
            val t0 = i.toDouble() / slices
            val t1 = (i + 1).toDouble() / slices
            val sliceMs = ms * (i + 1) / slices - ms * i / slices
            val more = i < slices - 1
            val previous = strokes
            strokes = paths.mapIndexed { k, points ->
                val part = Paths.slice(points, t0, t1)
                val path = Path().apply {
                    moveTo(part[0].x, part[0].y)
                    for (p in part.drop(1)) lineTo(p.x, p.y)
                }
                previous?.get(k)?.continueStroke(path, 0, sliceMs, more) ?: GestureDescription.StrokeDescription(path, 0, sliceMs, more)
            }
            val builder = GestureDescription.Builder()
            strokes.forEach(builder::addStroke)
            val started = android.os.SystemClock.uptimeMillis()
            dispatch(s, builder.build())
            // A finger held still sends nothing after its first touch, so Android calls a slice that continues done at
            // once (found on the phone): wait out the slice's time, the finger still down, before the next.
            if (more) delay(sliceMs - (android.os.SystemClock.uptimeMillis() - started))
            if (slices > 1) Log.i("PhoneRelay", "gesture slice ${i + 1}/$slices of $sliceMs ms took ${android.os.SystemClock.uptimeMillis() - started} ms")
        }
    }

    private suspend fun dispatch(s: AccessibilityService, gesture: GestureDescription) {
        suspendCancellableCoroutine { cont ->
            val dispatched = s.dispatchGesture(gesture, object : AccessibilityService.GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) = cont.resume(Unit)
                override fun onCancelled(gestureDescription: GestureDescription?) =
                    cont.resumeWithException(CommandError("gesture_cancelled", "Android cancelled the gesture (a real touch, or the screen changed)"))
            }, null)
            if (!dispatched) cont.resumeWithException(CommandError("device_error", "Android did not take the gesture"))
        }
    }

    override fun globalAction(action: GlobalAction) {
        val id = when (action) {
            GlobalAction.BACK -> AccessibilityService.GLOBAL_ACTION_BACK
            GlobalAction.HOME -> AccessibilityService.GLOBAL_ACTION_HOME
            GlobalAction.RECENTS -> AccessibilityService.GLOBAL_ACTION_RECENTS
            GlobalAction.NOTIFICATIONS -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
            GlobalAction.QUICK_SETTINGS -> AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS
            GlobalAction.POWER_DIALOG -> AccessibilityService.GLOBAL_ACTION_POWER_DIALOG
            GlobalAction.LOCK_SCREEN -> AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN
            GlobalAction.SCREENSHOT -> AccessibilityService.GLOBAL_ACTION_TAKE_SCREENSHOT
            GlobalAction.DISMISS_SHADE -> AccessibilityService.GLOBAL_ACTION_DISMISS_NOTIFICATION_SHADE
            GlobalAction.HEADSET_HOOK -> AccessibilityService.GLOBAL_ACTION_KEYCODE_HEADSETHOOK
            GlobalAction.DPAD_UP -> AccessibilityService.GLOBAL_ACTION_DPAD_UP
            GlobalAction.DPAD_DOWN -> AccessibilityService.GLOBAL_ACTION_DPAD_DOWN
            GlobalAction.DPAD_LEFT -> AccessibilityService.GLOBAL_ACTION_DPAD_LEFT
            GlobalAction.DPAD_RIGHT -> AccessibilityService.GLOBAL_ACTION_DPAD_RIGHT
            GlobalAction.DPAD_CENTER -> AccessibilityService.GLOBAL_ACTION_DPAD_CENTER
        }
        if (!service.performGlobalAction(id)) throw CommandError("device_error", "Android refused ${action.name.lowercase()}")
    }

    // ---- text fields ----

    /** The editable node with input focus, in any window: a popup (Chrome's suggestions) can be the active one. */
    private fun focusedNode(): AccessibilityNodeInfo? {
        val s = service
        s.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }?.let { return it }
        return s.windows.firstNotNullOfOrNull { w -> w.root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable } }
    }

    override fun focusedField(): Field? {
        val n = focusedNode() ?: return null
        return Field(
            pkg = n.packageName?.toString(),
            inputType = n.inputType,
            password = n.isPassword,
            multiLine = n.isMultiLine,
            text = if (n.isShowingHintText) "" else n.text?.toString().orEmpty(),
            selectionStart = n.textSelectionStart,
            selectionEnd = n.textSelectionEnd,
        )
    }

    private fun focusedOrFail() = focusedNode() ?: throw CommandError("no_text_field", "no text field has focus")

    /** The accessibility service's own input connection to the focused field (flagInputMethodEditor), if started. */
    private fun inputConnection(): android.accessibilityservice.InputMethod.AccessibilityInputConnection? {
        val im = PhoneAccessibilityService.instance?.inputMethod ?: return null
        if (!im.currentInputStarted) return null
        return im.currentInputConnection
    }

    override fun commitText(text: String): Boolean {
        val ic = inputConnection() ?: return false
        ic.commitText(text, 1, null)
        return true
    }

    override fun sendKey(keyName: String): Boolean {
        val ic = inputConnection() ?: return false
        val code = KeyEvent.keyCodeFromString(keyName)
        if (code == KeyEvent.KEYCODE_UNKNOWN) return false
        val now = android.os.SystemClock.uptimeMillis()
        ic.sendKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0))
        ic.sendKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, code, 0))
        return true
    }

    override fun setFieldText(text: String, cursor: Int) {
        val n = focusedOrFail()
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }
        if (!n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) throw CommandError("device_error", "the field refused the text")
        moveCursor(cursor, n)
    }

    override fun moveCursor(position: Int) = moveCursor(position, focusedOrFail())

    private fun moveCursor(position: Int, n: AccessibilityNodeInfo) {
        val args = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, position)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, position)
        }
        n.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, args)
    }

    override fun imeEnter() {
        val n = focusedOrFail()
        if (!n.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) {
            throw CommandError("device_error", "the field has no keyboard action to run")
        }
    }

    override fun scroll(forward: Boolean) {
        val root = service.rootInActiveWindow ?: throw CommandError("device_error", "nothing is on the screen")
        val target = firstScrollable(root) ?: throw CommandError("device_error", "nothing on the screen scrolls")
        val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        if (!target.performAction(action)) throw CommandError("device_error", "it would not scroll further")
    }

    private fun firstScrollable(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val queue = ArrayDeque(listOf(root))
        while (queue.isNotEmpty()) {
            val n = queue.removeFirst()
            if (n.isScrollable && n.isVisibleToUser) return n
            for (i in 0 until n.childCount) n.getChild(i)?.let(queue::addLast)
        }
        return null
    }

    override fun paste(text: String) {
        val n = focusedOrFail()
        setClipboard(text)
        if (!n.performAction(AccessibilityNodeInfo.ACTION_PASTE)) throw CommandError("device_error", "the field refused the paste")
    }

    override fun setClipboard(text: String?) {
        val clipboard = context.getSystemService(ClipboardManager::class.java)
        if (text == null) clipboard.clearPrimaryClip() else clipboard.setPrimaryClip(ClipData.newPlainText("Phone Relay", text))
    }

    override suspend fun readClipboard(): String? {
        val s = service
        val result = CompletableDeferred<String?>()
        ClipboardActivity.pending = result
        try {
            // Android lets only the app in front read the clipboard: an invisible window comes forward for a moment.
            s.startActivity(
                Intent(context, ClipboardActivity::class.java).addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION or Intent.FLAG_ACTIVITY_NO_USER_ACTION,
                ),
            )
            // In a list: an empty clipboard's null is an answer, the timeout's null is not.
            val read = withTimeoutOrNull(CLIPBOARD_MS) { listOf(result.await()) }
                ?: throw CommandError("device_error", "the app could not come forward to read the clipboard (Android lets only the app in front read it)")
            return read[0]
        } finally {
            if (ClipboardActivity.pending === result) ClipboardActivity.pending = null
        }
    }

    // ---- recording and sound ----

    override suspend fun startRecording(seconds: Double, size: Size?, bitRate: Int) = capture.startRecording(seconds, size, bitRate)

    override suspend fun stopRecording(): String? = capture.stopRecording()

    override suspend fun recordAudio(seconds: Double, source: String): String = capture.recordAudio(seconds, source)

    override fun ownFileSize(path: String): Long = File(path).length()

    override fun endCapture() = capture.end()

    // ---- the shell bridge ----

    override suspend fun shell(command: String, guard: (() -> Unit)?): String = adb.run(command, guard)

    override suspend fun shellWithInput(command: String, inputPath: String, guard: (() -> Unit)?) = adb.runWithInput(command, File(inputPath), guard)

    override fun newOwnFile(): String = File.createTempFile("upload", ".part", context.cacheDir).path

    override suspend fun shellToFile(command: String, guard: (() -> Unit)?): String {
        val file = File.createTempFile("shell", ".out", context.cacheDir)
        try {
            adb.runToFile(command, file, guard)
        } catch (e: Throwable) { // a cancellation too
            file.delete()
            throw e
        }
        return file.path
    }

    override suspend fun <T> devtools(socket: String, work: suspend (DevToolsLink) -> T): T {
        val bridge = DevToolsBridge(adb, socket, version)
        try {
            return work(bridge)
        } finally {
            bridge.close()
        }
    }

    // ---- sound, power ----

    override fun adjustVolume(direction: Int) {
        context.getSystemService(AudioManager::class.java)
            .adjustSuggestedStreamVolume(direction, AudioManager.USE_DEFAULT_STREAM_TYPE, AudioManager.FLAG_SHOW_UI)
    }

    override fun mediaKey(keyName: String) {
        val code = KeyEvent.keyCodeFromString(keyName)
        if (code == KeyEvent.KEYCODE_UNKNOWN) throw CommandError("invalid_args", "no key $keyName")
        val audio = context.getSystemService(AudioManager::class.java)
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
    }

    @Suppress("DEPRECATION")
    override fun wake() {
        val power = context.getSystemService(PowerManager::class.java)
        // The one way an app turns the screen on; it does not unlock the phone.
        power.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "PhoneRelay:wake").acquire(3000)
    }

    // ---- apps ----

    /** Activities start from the accessibility service: an app with one bound may start them from the background. */
    private val starter: Context get() = PhoneAccessibilityService.instance ?: context

    override fun launch(pkg: String) {
        val intent = context.packageManager.getLaunchIntentForPackage(pkg)
            ?: throw CommandError("launch_failed", "$pkg has no launcher activity (or is not installed)")
        try {
            starter.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            throw CommandError("launch_failed", "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    override fun startIntent(spec: IntentSpec): String {
        val intent = Intent()
        spec.action?.let { intent.action = it }
        when {
            spec.data != null && spec.type != null -> intent.setDataAndType(Uri.parse(spec.data), spec.type)
            spec.data != null -> intent.data = Uri.parse(spec.data)
            spec.type != null -> intent.type = spec.type
        }
        spec.component?.let {
            intent.component = ComponentName.unflattenFromString(it) ?: throw CommandError("invalid_args", "component must look like package/.Activity")
        }
        spec.pkg?.let { intent.setPackage(it) }
        spec.categories.forEach { intent.addCategory(it) }
        spec.flags?.let { intent.addFlags(it) }
        putExtras(intent, spec.extras)
        try {
            if (spec.broadcast) starter.sendBroadcast(intent)
            else starter.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: ActivityNotFoundException) {
            throw CommandError("launch_failed", "nothing on the phone handles $intent")
        } catch (e: SecurityException) {
            throw CommandError("launch_failed", "not allowed: ${e.message}")
        }
        return (if (spec.broadcast) "Broadcasting: " else "Starting: ") + intent.toString()
    }

    private fun putExtras(intent: Intent, extras: JsonObject) {
        for ((key, value) in extras) {
            when (val v = extraValue(key, value)) {
                null -> intent.putExtra(key, null as String?)
                is String -> intent.putExtra(key, v)
                is Int -> intent.putExtra(key, v)
                is Long -> intent.putExtra(key, v)
                is Double -> intent.putExtra(key, v.toFloat()) // as `am --ef`
                is Boolean -> intent.putExtra(key, v)
            }
        }
    }

    // ---- packages ----

    override fun apkInfo(path: String): ApkInfo {
        if (!File(path).canRead()) unreadable(path)
        val info = context.packageManager.getPackageArchiveInfo(path, PackageManager.PackageInfoFlags.of(0))
            ?: throw CommandError("invalid_args", "$path is not an APK Android can read")
        return ApkInfo(info.packageName, info.versionName, info.longVersionCode)
    }

    override fun packageDetails(name: String): PackageDetails {
        val pm = context.packageManager
        val info = try {
            pm.getPackageInfo(name, PackageManager.PackageInfoFlags.of(PackageManager.GET_SIGNING_CERTIFICATES.toLong()))
        } catch (e: PackageManager.NameNotFoundException) {
            throw CommandError("not_found", "$name is not installed")
        }
        val app = info.applicationInfo ?: throw CommandError("not_found", "$name has no application")
        // As `dumpsys package` prints its times, which logcat's -t takes.
        val time = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
        val sha256 = MessageDigest.getInstance("SHA-256")
        return PackageDetails(
            name = name,
            versionName = info.versionName,
            versionCode = info.longVersionCode,
            firstInstallTime = time.format(Date(info.firstInstallTime)),
            lastUpdateTime = time.format(Date(info.lastUpdateTime)),
            signingSha256 = info.signingInfo?.apkContentsSigners.orEmpty().map { sig ->
                sha256.digest(sig.toByteArray()).joinToString("") { "%02x".format(it) }
            }.distinct(),
            debuggable = app.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0,
            enabledState = when (pm.getApplicationEnabledSetting(name)) {
                PackageManager.COMPONENT_ENABLED_STATE_DEFAULT -> "default"
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED -> "enabled"
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED -> "disabled"
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED_USER -> "disabled_user"
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED_UNTIL_USED -> "disabled_until_used"
                else -> "unknown"
            },
            apkPath = app.sourceDir,
        )
    }

    override fun notifications(): List<JsonObject> =
        NotificationReader.instance?.active()
            ?: missing("allow notification access for Phone Relay (the app's own screen has the button)")

    // ---- files ----

    private fun entry(f: File): FileEntry = FileEntry(
        name = f.name.ifEmpty { "/" },
        type = when {
            Files.isSymbolicLink(f.toPath()) -> "link"
            f.isDirectory -> "dir"
            else -> "file"
        },
        size = if (f.isDirectory) 4096 else f.length(),
        modified = f.lastModified() / 1000,
    )

    private fun unreadable(path: String): Nothing {
        if (!Environment.isExternalStorageManager() && (path.startsWith("/sdcard") || path.startsWith("/storage"))) {
            missing<Unit>("allow All files access for Phone Relay (the app's own screen has the button)")
        }
        throw CommandError("not_found", "$path does not exist or the app cannot read it")
    }

    override fun listFiles(path: String): List<FileEntry> {
        val f = File(path)
        if (!f.exists()) unreadable(path)
        if (!f.isDirectory) return listOf(entry(f))
        val children = f.listFiles() ?: unreadable(path)
        return children.sortedBy { it.name }.map(::entry)
    }

    override fun stat(path: String): FileEntry = File(path).let { if (it.exists()) entry(it) else unreadable(path) }

    override fun openRead(path: String): InputStream = try {
        FileInputStream(path)
    } catch (e: FileNotFoundException) {
        unreadable(path)
    }

    override fun openWrite(path: String): OutputStream = try {
        File(path).parentFile?.mkdirs()
        FileOutputStream(path)
    } catch (e: FileNotFoundException) {
        if (!Environment.isExternalStorageManager()) missing<Unit>("allow All files access for Phone Relay (the app's own screen has the button)")
        throw CommandError("not_found", "${e.message}")
    }

    override fun moveFile(from: String, to: String) {
        val source = File(from)
        val target = File(to)
        if (!source.renameTo(target)) {
            // renameTo will not replace on every filesystem: replace by hand.
            if (target.exists() && !target.delete()) throw CommandError("device_error", "could not replace $to")
            if (!source.renameTo(target)) throw CommandError("device_error", "could not move the upload to $to")
        }
    }

    override fun deleteFile(path: String) {
        File(path).delete()
    }

    override fun scanFile(path: String) {
        MediaScannerConnection.scanFile(context, arrayOf(path), null, null)
    }

    override suspend fun ask(reason: String, steps: List<String>, seconds: Double) = RequestPrompt.ask(context, reason, steps, seconds)

    private companion object {
        const val CLIPBOARD_MS = 5_000L
    }
}
