package com.thomaswcode.phonerelay.commands

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** A phone in memory: records what the commands did to it. */
class FakeDevice : Device {
    var fg: String? = "com.android.chrome"
    var isAwake = true
    var field: Field? = Field("com.android.chrome", 0x1, password = false, multiLine = false, text = "", selectionStart = 0, selectionEnd = 0)
    var rotationNow = Rotation(true, 0)
    var canWriteSettings = true
    var shade = 40
    var gestureDelayMs = 0L
    var notificationAccess = true
    val gestures = mutableListOf<Pair<List<List<Point>>, Long>>()
    val globals = mutableListOf<GlobalAction>()
    val imeEnters = mutableListOf<String>()
    val pasted = mutableListOf<String>()
    val intents = mutableListOf<IntentSpec>()
    val launched = mutableListOf<String>()
    var clip: String? = "old"
    val files = mutableMapOf("/sdcard/Download/a.txt" to "hello file".toByteArray())
    val written = mutableMapOf<String, ByteArray>()
    val scanned = mutableListOf<String>()
    var roots: List<UiNode> = listOf(node("com.android.chrome", "", children = listOf(
        node("com.android.chrome", "Hello & welcome", bounds = intArrayOf(50, 400, 1030, 500)),
        node("com.android.chrome", "", desc = "Search or type URL", id = "com.android.chrome:id/url_bar", cls = "android.widget.EditText",
            editable = true, focused = true, bounds = intArrayOf(100, 120, 900, 220)),
        node("com.android.chrome", "hidden", visible = false, children = listOf(node("com.android.chrome", "under hidden"))),
    )))

    override fun foreground() = fg
    override fun awake() = isAwake
    override fun battery() = 87
    override fun screenSize() = Size(1080, 2340)
    override fun rotation() = rotationNow
    override fun setRotation(rotation: Int?) {
        if (!canWriteSettings) throw CommandError("permission_missing", "allow Modify system settings")
        rotationNow = if (rotation == null) rotationNow.copy(auto = true) else Rotation(false, rotation)
    }

    override suspend fun screenshot(scale: Double): Shot {
        val w = Math.round(1080 * scale).toInt()
        val h = Math.round(2340 * scale).toInt()
        return Shot(byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 1, 2), w, h, 1080, 2340, scale)
    }

    override suspend fun frame(): Frame = Frame(4, 4, ByteArray(16) { shade.toByte() })
    override fun uiRoots() = roots

    override suspend fun gesture(paths: List<List<Point>>, ms: Long) {
        if (gestureDelayMs > 0) delay(gestureDelayMs)
        gestures += paths to ms
    }

    override fun globalAction(action: GlobalAction) {
        globals += action
        if (action == GlobalAction.HOME) fg = "com.sec.android.app.launcher"
    }

    /** Whether the fake has an input connection (the accessibility IME), and what it was given. */
    var ime = false
    val committed = mutableListOf<String>()
    val keysSent = mutableListOf<String>()
    override fun commitText(text: String): Boolean {
        if (!ime) return false
        committed += text
        return true
    }

    override fun sendKey(keyName: String): Boolean {
        if (!ime) return false
        keysSent += keyName
        return true
    }

    override fun focusedField() = field
    override fun setFieldText(text: String, cursor: Int) {
        field = field!!.copy(text = text, selectionStart = cursor, selectionEnd = cursor)
    }

    override fun moveCursor(position: Int) {
        field = field!!.copy(selectionStart = position, selectionEnd = position)
    }

    override fun imeEnter() {
        imeEnters += field!!.text
    }

    val scrolls = mutableListOf<Boolean>()
    override fun scroll(forward: Boolean) {
        scrolls += forward
    }

    override fun paste(text: String) {
        pasted += text
    }

    override fun setClipboard(text: String?) {
        clip = text
    }

    val volume = mutableListOf<Int>()
    override fun adjustVolume(direction: Int) {
        volume += direction
    }

    val media = mutableListOf<String>()
    override fun mediaKey(keyName: String) {
        media += keyName
    }

    var woken = 0
    override fun wake() {
        woken++
    }

    override fun launch(pkg: String) {
        if (pkg == "com.example.missing") throw CommandError("launch_failed", "no launcher activity")
        launched += pkg
        fg = pkg
    }

    override fun startIntent(spec: IntentSpec): String {
        if (spec.data?.startsWith("nosuchscheme:") == true) throw CommandError("launch_failed", "nothing handles it")
        intents += spec
        return "Starting: $spec"
    }

    override fun notifications(): List<JsonObject> {
        if (!notificationAccess) throw CommandError("permission_missing", "allow notification access")
        return listOf(buildJsonObject { put("key", "0|com.google.android.gm|4"); put("pkg", "com.google.android.gm"); put("title", "A sender") })
    }

    override fun listFiles(path: String): List<FileEntry> {
        if (path == "/sdcard/Download") return listOf(FileEntry("a.txt", "file", 10, 1700000000))
        if (path in files) return listOf(stat(path))
        throw CommandError("not_found", path)
    }

    override fun stat(path: String): FileEntry = when {
        path in files -> FileEntry(path.substringAfterLast('/'), "file", files[path]!!.size.toLong(), 1700000000)
        path == "/sdcard/Download" -> FileEntry("Download", "dir", 4096, 1700000000)
        else -> throw CommandError("not_found", path)
    }

    override fun openRead(path: String): InputStream = ByteArrayInputStream(files[path] ?: throw CommandError("not_found", path))
    override fun openWrite(path: String): OutputStream = object : ByteArrayOutputStream() {
        override fun close() {
            written[path] = toByteArray()
        }
    }

    override fun scanFile(path: String) {
        scanned += path
    }

    companion object {
        fun node(
            pkg: String, text: String, desc: String = "", id: String = "", cls: String = "android.widget.TextView",
            editable: Boolean = false, focused: Boolean = false, visible: Boolean = true,
            bounds: IntArray = intArrayOf(0, 80, 1080, 2340), children: List<UiNode> = emptyList(),
        ): UiNode = object : UiNode {
            override val text = text
            override val desc = desc
            override val viewId = id
            override val className = cls
            override val pkg = pkg
            override val bounds = bounds
            override val clickable = editable
            override val scrollable = false
            override val editable = editable
            override val focused = focused
            override val password = false
            override val checked = false
            override val visible = visible
            override val children = children
        }
    }
}
