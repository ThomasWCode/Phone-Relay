package com.thomaswcode.phonerelay.commands

import java.io.InputStream
import java.io.OutputStream
import kotlinx.serialization.json.JsonObject

/** A refusal or failure, sent to the relay as {ok: false, error: code, detail}. */
class CommandError(val code: String, val detail: String = "") : Exception(if (detail.isEmpty()) code else "$code: $detail")

data class Size(val width: Int, val height: Int)

data class Rotation(val auto: Boolean?, val userRotation: Int?)

/** A screenshot as the relay sends it: JPEG bytes, the image's size, the screen's size, the scale used. */
class Shot(val jpeg: ByteArray, val width: Int, val height: Int, val realWidth: Int, val realHeight: Int, val scale: Double)

/** A small greyscale copy of the screen, for telling whether it changed. */
class Frame(val width: Int, val height: Int, val grey: ByteArray)

data class Point(val x: Float, val y: Float)

/** The text field with input focus, as the accessibility framework reports it. */
data class Field(
    val pkg: String?,
    val inputType: Int,
    val password: Boolean,
    val multiLine: Boolean,
    /** Its text, or "" when it only shows a hint. */
    val text: String,
    /** The selection, or -1 when unknown (then text goes at the end). */
    val selectionStart: Int,
    val selectionEnd: Int,
)

/** The phone-wide actions an accessibility service can take (AccessibilityService.GLOBAL_ACTION_*). */
enum class GlobalAction { BACK, HOME, RECENTS, NOTIFICATIONS, QUICK_SETTINGS, DISMISS_SHADE, POWER_DIALOG, LOCK_SCREEN, SCREENSHOT, HEADSET_HOOK, DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, DPAD_CENTER }

/** An intent built from /open's or /intent's fields. Extras keep their JSON types. */
data class IntentSpec(
    val action: String? = null,
    val data: String? = null,
    val type: String? = null,
    val component: String? = null,
    val pkg: String? = null,
    val categories: List<String> = emptyList(),
    val flags: Int? = null,
    val extras: JsonObject = JsonObject(emptyMap()),
    val broadcast: Boolean = false,
)

data class FileEntry(val name: String, val type: String, val size: Long, val modified: Long)

/** A node of the UI tree, as far as the relay's elements need it. */
interface UiNode {
    val text: String?
    val desc: String?
    val viewId: String?
    val className: String?
    val pkg: String?
    /** left, top, right, bottom in screen pixels */
    val bounds: IntArray
    val clickable: Boolean
    val scrollable: Boolean
    val editable: Boolean
    val focused: Boolean
    val password: Boolean
    val checked: Boolean
    val visible: Boolean
    val children: List<UiNode>
}

/**
 * What the phone can do, as the commands need it. The app implements it with its accessibility
 * service and Android's own APIs (AndroidDevice); tests with a fake. Each method throws
 * CommandError for what the phone refuses (a missing permission, nothing to launch, ...).
 */
interface Device {
    /** The package of the window in front (com.android.systemui for the lock screen and the shade), or null. */
    fun foreground(): String?
    fun awake(): Boolean
    fun battery(): Int?
    fun screenSize(): Size
    fun rotation(): Rotation
    /** A quarter-turn lock (0-3), or null for automatic rotation. */
    fun setRotation(rotation: Int?)
    suspend fun screenshot(scale: Double): Shot
    suspend fun frame(): Frame
    /** Each window's root, bottom window first. */
    fun uiRoots(): List<UiNode>
    /** Every pointer down at its path's first point, along its path over ms, then up. */
    suspend fun gesture(paths: List<List<Point>>, ms: Long)
    fun globalAction(action: GlobalAction)
    fun focusedField(): Field?
    /**
     * Types `text` at the focused field's cursor through the accessibility service's own input
     * connection, as a keyboard does. False when there is none (no field has input focus).
     */
    fun commitText(text: String): Boolean
    /** Presses a key (a KEYCODE_* name) in the focused field, as a keyboard does. False without an input connection. */
    fun sendKey(keyName: String): Boolean
    /** Replaces the focused field's text and puts the cursor at `cursor` (the fallback when there is no input connection). */
    fun setFieldText(text: String, cursor: Int)
    fun moveCursor(position: Int)
    /** The keyboard's action key (Go, Search, Send, Done) on the focused field. */
    fun imeEnter()
    /** Scrolls the scrollable node in front forward (down) or back. */
    fun scroll(forward: Boolean)
    /** Puts `text` on the clipboard and pastes it into the focused field. */
    fun paste(text: String)
    /** Puts `text` on the clipboard, or empties it for null. */
    fun setClipboard(text: String?)
    /** The clipboard's text, or null. Android lets only the app in front read it, so the app comes forward for a moment. */
    suspend fun readClipboard(): String?
    /** Starts recording the screen to a file, for at most `seconds`. */
    suspend fun startRecording(seconds: Double, size: Size?, bitRate: Int)
    /** Stops the recording and gives its MP4 file's path, or null if there is none. */
    suspend fun stopRecording(): String?
    /** Records `seconds` of the phone's sound output ("output") or its microphone ("mic") to a WAV file; its path. */
    suspend fun recordAudio(seconds: Double, source: String): String
    /** The size of a file the app wrote for itself (a recording); openRead and deleteFile take it too. */
    fun ownFileSize(path: String): Long
    /** Stops any recording and deletes it: the owner's kill switches and a closed connection end capture. */
    fun endCapture()
    /** Runs a command as the shell user through the app's own adb connection (the shell bridge); its output. */
    suspend fun shell(command: String): String
    /** Runs `work` on a DevTools socket (chrome_devtools_remote, or a debuggable WebView's) through the shell bridge. */
    suspend fun <T> devtools(socket: String, work: suspend (DevToolsLink) -> T): T
    /** AudioManager.ADJUST_RAISE (1), ADJUST_LOWER (-1), ADJUST_TOGGLE_MUTE (101). */
    fun adjustVolume(direction: Int)
    fun mediaKey(keyName: String)
    fun wake()
    fun launch(pkg: String)
    fun startIntent(spec: IntentSpec): String
    fun notifications(): List<JsonObject>
    fun listFiles(path: String): List<FileEntry>
    fun stat(path: String): FileEntry
    fun openRead(path: String): InputStream
    fun openWrite(path: String): OutputStream
    /** Moves a file over another (replacing it), within one filesystem. */
    fun moveFile(from: String, to: String)
    fun deleteFile(path: String)
    /** Tells the media library about a file written to shared storage. */
    fun scanFile(path: String)
}
