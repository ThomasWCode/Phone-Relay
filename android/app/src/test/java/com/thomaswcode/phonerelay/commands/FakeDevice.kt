package com.thomaswcode.phonerelay.commands

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
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
    val files = mutableMapOf("/sdcard/Download/a.txt" to "hello file".toByteArray(), TINY_INSTALLED to "fake apk bytes".toByteArray())
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

    override suspend fun readClipboard(): String? = clip

    val recordings = mutableListOf<Triple<Double, Size?, Int>>()
    var recordingNow = false
    var captureEnded = 0
    override suspend fun startRecording(seconds: Double, size: Size?, bitRate: Int) {
        recordings += Triple(seconds, size, bitRate)
        files[RECORDING] = "mp4 bytes".toByteArray()
        recordingNow = true
    }

    override suspend fun stopRecording(): String? {
        if (!recordingNow) return null
        recordingNow = false
        return RECORDING
    }

    val audioTaken = mutableListOf<Pair<Double, String>>()
    override suspend fun recordAudio(seconds: Double, source: String): String {
        audioTaken += seconds to source
        files[AUDIO] = Wav.header(4) + ByteArray(4)
        return AUDIO
    }

    override fun ownFileSize(path: String): Long = files[path]?.size?.toLong() ?: 0

    override fun endCapture() {
        captureEnded++
        recordingNow = false
        files.remove(RECORDING)
    }

    /** The shell bridge: off (not paired) unless a test pairs it; what it was told to run. */
    var shellPaired = false
    val shellCommands = mutableListOf<String>()
    val devtoolsSockets = mutableListOf<String>()
    val chrome = FakeChrome()
    /** Runs while the bridge is "set up", before the guard: a test changes the foreground there. */
    var duringShellSetup: (() -> Unit)? = null
    /** What the shell prints for a command (null: nothing), and the bytes shellToFile gets. */
    var shellReply: (String) -> String? = { null }
    var shellBytes: (String) -> ByteArray = { ByteArray(0) }
    override suspend fun shell(command: String, guard: (() -> Unit)?): String {
        if (!shellPaired) throw CommandError("shell_unavailable", "pair the app with wireless debugging first")
        duringShellSetup?.invoke()
        guard?.invoke()
        shellCommands += command
        return shellReply(command) ?: if (command == "cat /proc/net/unix") UNIX_SOCKETS else ""
    }

    override suspend fun shellToFile(command: String, guard: (() -> Unit)?): String {
        if (!shellPaired) throw CommandError("shell_unavailable", "pair the app with wireless debugging first")
        guard?.invoke()
        shellCommands += command
        files[SHELL_OUT] = shellBytes(command)
        return SHELL_OUT
    }

    /** What shellWithInput was given: (command, input). */
    val shellInputs = mutableListOf<Pair<String, ByteArray>>()
    override suspend fun shellWithInput(command: String, inputPath: String, guard: (() -> Unit)?) {
        if (!shellPaired) throw CommandError("shell_unavailable", "pair the app with wireless debugging first")
        guard?.invoke()
        shellCommands += command
        shellInputs += command to (written[inputPath] ?: throw CommandError("not_found", inputPath))
    }

    override fun newOwnFile(): String = UPLOAD

    /** APK files on the phone, and the packages installed. */
    val apks = mutableMapOf("/sdcard/Download/tiny.apk" to ApkInfo("com.example.tiny", "1.2.3", 42))
    val packages = mutableMapOf(
        "com.example.tiny" to PackageDetails(
            "com.example.tiny", "1.2.2", 41, "2026-10-01 10:00:00", "2026-11-09 06:58:42", listOf("ab12"), debuggable = true,
            enabledState = "default", apkPath = TINY_INSTALLED,
        ),
        "com.android.chrome" to PackageDetails(
            "com.android.chrome", "140.0", 700000000, "2026-01-01 00:00:00", "2026-11-01 00:00:00", listOf("cd34"), debuggable = false,
            enabledState = "default", apkPath = "/data/app/~~Y2hy==/com.android.chrome-Y2hy==/base.apk",
        ),
    )
    override fun apkInfo(path: String): ApkInfo = apks[path] ?: throw CommandError("not_found", "$path does not exist or the app cannot read it")
    override fun packageDetails(name: String): PackageDetails = packages[name] ?: throw CommandError("not_found", "$name is not installed")

    override suspend fun <T> devtools(socket: String, work: suspend (DevToolsLink) -> T): T {
        if (!shellPaired) throw CommandError("shell_unavailable", "pair the app with wireless debugging first")
        devtoolsSockets += socket
        return work(chrome)
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

    override fun moveFile(from: String, to: String) {
        written[to] = written.remove(from) ?: throw CommandError("not_found", from)
    }

    override fun deleteFile(path: String) {
        written.remove(path)
        if (path == RECORDING || path == AUDIO || path == SHELL_OUT) files.remove(path)
    }

    override fun scanFile(path: String) {
        scanned += path
    }

    /** The owner's answer to the next request, and what each request showed them (reason, steps, seconds). */
    var answer = Answer(true, null)
    val asked = mutableListOf<Triple<String, List<String>, Double>>()
    override suspend fun ask(reason: String, steps: List<String>, seconds: Double): Answer {
        asked += Triple(reason, steps, seconds)
        return answer
    }

    companion object {
        const val UNIX_SOCKETS = """Num       RefCount Protocol Flags    Type St Inode Path
0000000000000000: 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote
0000000000000000: 00000002 00000000 00010000 0001 01 12346 @webview_devtools_remote_4321
0000000000000000: 00000002 00000000 00010000 0001 01 12347 @some_other_socket
0000000000000000: 00000002 00000000 00010000 0001 01 12348 @chrome_devtools_remote
"""
        const val RECORDING = "/data/user/0/com.thomaswcode.phonerelay/cache/record.mp4"
        const val AUDIO = "/data/user/0/com.thomaswcode.phonerelay/cache/audio.wav"
        const val SHELL_OUT = "/data/user/0/com.thomaswcode.phonerelay/cache/shell.out"
        const val UPLOAD = "/data/user/0/com.thomaswcode.phonerelay/cache/upload.part"
        const val TINY_INSTALLED = "/data/app/~~Zm9v==/com.example.tiny-YmFy==/base.apk"

        fun node(
            pkg: String, text: String, desc: String = "", id: String = "", cls: String = "android.widget.TextView",
            editable: Boolean = false, focused: Boolean = false, visible: Boolean = true,
            bounds: IntArray = intArrayOf(0, 80, 1080, 2340), children: List<UiNode> = emptyList(), clickable: Boolean = editable,
        ): UiNode = object : UiNode {
            override val text = text
            override val desc = desc
            override val viewId = id
            override val className = cls
            override val pkg = pkg
            override val bounds = bounds
            override val clickable = clickable
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

/** Chrome's DevTools endpoint in memory: two tabs, the browser target, and the calls made to it. */
class FakeChrome : DevToolsLink {
    val tabs = mutableListOf("A" to "https://example.org/one", "B" to "https://example.org/two")
    val calls = mutableListOf<Triple<String, String, JsonObject>>()

    override suspend fun http(path: String, method: String): JsonElement = when {
        path == "/json/list" -> JsonArray(tabs.map { (id, url) ->
            buildJsonObject { put("id", id); put("type", "page"); put("title", "Tab $id"); put("url", url); put("webSocketDebuggerUrl", "ws://x/devtools/page/$id") }
        } + buildJsonObject { put("id", "W"); put("type", "service_worker"); put("title", "worker"); put("url", "https://example.org/sw.js") })
        path == "/json/version" -> buildJsonObject { put("webSocketDebuggerUrl", "ws://127.0.0.1/devtools/browser") }
        path.startsWith("/json/close/") -> { tabs.removeAll { it.first == path.substringAfterLast('/') }; JsonPrimitive("Target is closing") }
        path.startsWith("/json/activate/") -> JsonPrimitive("Target activated")
        else -> throw CommandError("devtools_failed", "$method $path: HTTP 404")
    }

    override suspend fun cdp(path: String, method: String, params: JsonObject): JsonObject {
        calls += Triple(path, method, params)
        return when (method) {
            "Runtime.evaluate" -> if ((params["expression"] as JsonPrimitive).content == "throw 1") {
                buildJsonObject { put("exceptionDetails", buildJsonObject { put("text", "Uncaught"); put("exception", buildJsonObject { put("description", "1") }) }) }
            } else buildJsonObject { put("result", buildJsonObject { put("type", "number"); put("value", 2); put("description", "2") }) }
            "Target.createTarget" -> {
                tabs.add(0, "NEW" to (params["url"] as JsonPrimitive).content)
                buildJsonObject { put("targetId", "NEW") }
            }
            else -> buildJsonObject { put("ok", true) }
        }
    }
}
