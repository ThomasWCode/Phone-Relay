package com.thomaswcode.phonerelay.commands

import java.net.URLConnection
import java.util.Base64
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.yield
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put

/**
 * The relay's commands, run on the phone by the app. Same names, arguments and replies as the adb
 * client (phone/phone_client.py), done with what an app can use: its accessibility service
 * (gestures, global actions, the UI tree, screenshots, text fields) and Android's own APIs.
 *
 * Differences from the adb client: if_foreground is checked just before the action (the adb
 * client's check, in the same shell command, leaves a longer gap: `input` takes a moment to start);
 * typing goes through the accessibility service's own input connection to the focused field (as a
 * keyboard); keys are what an app can do (Keys), any key reaching a focused field; recording and
 * audio go through MediaProjection, whose consent prompt the accessibility service answers. What
 * only the shell user can do (Chrome DevTools, other keys, typing with no field focused) goes
 * through the shell bridge: the app's own adb connection to this phone (AdbShell), once paired. So
 * do the release checks that read the shell's view of the phone (installs, accessibility, logcat,
 * settings, run-as, intents as the shell user); what is installed of a package and its APK come
 * from PackageManager, so they work off Wi-Fi too.
 */
class Commands(
    private val device: Device,
    private val version: String,
    /** A key number's KEYCODE_* name (Android's KeyEvent.keyCodeToString). */
    private val keyName: (Int) -> String?,
) {
    private val handlers: Map<String, suspend (JsonObject, Stream?) -> JsonObject> = mapOf(
        "state" to { a, _ -> state(a) },
        "screenshot" to { a, _ -> screenshot(a) },
        "ui" to { a, _ -> ui(a) },
        "tap" to { a, _ -> tap(a) },
        "swipe" to { a, _ -> swipe(a) },
        "gesture" to { a, _ -> gesture(a) },
        "type" to { a, _ -> type(a) },
        "key" to { a, _ -> key(a) },
        "launch" to { a, _ -> launch(a) },
        "open" to { a, _ -> open(a) },
        "intent" to { a, _ -> intent(a) },
        "notifications" to { _, _ -> notifications() },
        "statusbar" to { a, _ -> statusbar(a) },
        "rotation" to { a, _ -> rotation(a) },
        "clipboard_set" to { a, _ -> clipboardSet(a) },
        "clipboard_get" to { _, _ -> buildJsonObject { put("text", device.readClipboard()) } },
        "record_start" to { a, _ -> recordStart(a) },
        "record_stop" to { _, s -> recordStop(s ?: noStream("record_stop")) },
        "record" to { a, s -> record(a, s ?: noStream("record")) },
        "audio" to { a, s -> audio(a, s ?: noStream("audio")) },
        "chrome_sockets" to { _, _ ->
            buildJsonObject { put("sockets", JsonArray(DevTools.sockets(device.shell("cat /proc/net/unix", null)).map { JsonPrimitive(it) })) }
        },
        "chrome_tabs" to { a, _ -> device.devtools(DevTools.socket(a)) { DevTools.tabs(it) } },
        "chrome_eval" to { a, _ -> device.devtools(DevTools.socket(a)) { DevTools.eval(it, a) } },
        "chrome_cdp" to { a, _ -> device.devtools(DevTools.socket(a)) { DevTools.cdp(it, a) } },
        "chrome_new" to { a, _ -> device.devtools(DevTools.socket(a)) { DevTools.new(it, a) } },
        "chrome_close" to { a, _ -> device.devtools(DevTools.socket(a)) { DevTools.tabAction(it, a, "close") } },
        "chrome_activate" to { a, _ -> device.devtools(DevTools.socket(a)) { DevTools.tabAction(it, a, "activate") } },
        "wait" to { a, _ -> wait(a) },
        "batch" to { a, _ -> batch(a) },
        "request" to { a, _ -> request(a) },
        "files" to { a, _ -> files(a) },
        "pull" to { a, s -> pull(a, s ?: noStream("pull")) },
        "push" to { a, s -> push(a, s ?: noStream("push")) },
        "install" to { a, _ -> install(a) },
        "accessibility" to { _, _ -> accessibility() },
        "logcat" to { a, _ -> logcat(a) },
        "package" to { a, _ -> packageDetails(a) },
        "package_apk" to { a, s -> packageApk(a, s ?: noStream("package_apk")) },
        "run_as" to { a, s -> runAs(a, s ?: noStream("run_as")) },
        "run_as_files" to { a, _ -> runAsFiles(a) },
        "settings" to { a, _ -> settings(a) },
        "content" to { a, _ -> content(a) },
        "run_as_archive" to { a, s -> runAsArchive(a, s ?: noStream("run_as_archive")) },
        "run_as_push" to { a, s -> runAsPush(a, s ?: noStream("run_as_push")) },
        "run_as_delete" to { a, _ -> runAsDelete(a) },
        "run_as_kill" to { a, _ -> runAsKill(a) },
        "night_mode" to { a, _ -> nightMode(a) },
        "force_stop" to { a, _ -> forceStop(a) },
    )

    /** The commands this client runs, announced to the relay in hello and in /state. */
    val capabilities: List<String> = handlers.keys.sorted()

    suspend fun handle(cmd: String, args: JsonObject, stream: Stream? = null): JsonObject {
        val handler = handlers[cmd] ?: throw CommandError("invalid_args", "unknown command: $cmd")
        val packages = args.ifForeground()
        val timeout = args.timeoutArg(defaultTimeout(cmd, args))
        // Checked before the command starts; a batch (and a request's queued steps) checks each step instead.
        if (packages != null && cmd != "batch" && cmd != "request") requireForeground(packages)
        // withTimeoutOrNull: only this deadline turns into "timeout"; an outer one cancels as usual.
        return withTimeoutOrNull((timeout * 1000).toLong()) { handler(args, stream) }
            ?: throw CommandError("timeout", "$cmd took longer than ${fmt(timeout)} s; pass a larger timeout")
    }

    /** The connection closed (the owner's switch, a disable, a drop): capture ends with it, as in the adb client. */
    fun closed() = device.endCapture()

    private fun noStream(cmd: String): Nothing = throw CommandError("invalid_args", "$cmd streams over the relay; it cannot run here")

    private fun requireForeground(packages: List<String>) {
        val fg = device.foreground()
        val awake = device.awake()
        if (fg !in packages || !awake) {
            throw CommandError("foreground_mismatch", "wanted ${packages.joinToString(", ")}; foreground=$fg, awake=$awake")
        }
    }

    private fun recheck(args: JsonObject, what: String) {
        val packages = args.ifForeground() ?: return
        val fg = device.foreground()
        if (fg !in packages) throw CommandError("foreground_mismatch", "foreground changed to $fg during the $what")
    }

    // ---- see ----

    private fun state(@Suppress("UNUSED_PARAMETER") args: JsonObject): JsonObject {
        val size = device.screenSize()
        val rotation = device.rotation()
        return buildJsonObject {
            put("foreground", device.foreground())
            put("awake", device.awake())
            put("battery", device.battery())
            put("screen", buildJsonObject { put("width", size.width); put("height", size.height) })
            put("rotation", rotationJson(rotation))
            put("client", "android-app")
            put("client_version", version)
            put("capabilities", JsonArray(capabilities.map { JsonPrimitive(it) }))
        }
    }

    private fun rotationJson(r: Rotation) = buildJsonObject { put("auto", r.auto); put("user_rotation", r.userRotation) }

    private suspend fun screenshot(args: JsonObject): JsonObject {
        val scale = minOf(args.scaleArg(0.5), 1.0) // no upscaling: it adds no detail
        val shot = device.screenshot(scale)
        recheck(args, "capture")
        return buildJsonObject {
            put("mime", "image/jpeg")
            put("data", Base64.getEncoder().encodeToString(shot.jpeg))
            put("width", shot.width)
            put("height", shot.height)
            put("real_width", shot.realWidth)
            put("real_height", shot.realHeight)
            put("scale", shot.scale)
            put("foreground", device.foreground())
        }
    }

    private fun ui(args: JsonObject): JsonObject {
        // Both engines are the accessibility tree here: it unbinds no other accessibility service, unlike uiautomator.
        val engine = args.stringArg("engine", required = false) ?: "helper"
        if (engine != "helper" && engine != "uiautomator") throw CommandError("invalid_args", "engine must be helper or uiautomator")
        val elements = UiTree.elements(device.uiRoots())
        recheck(args, "UI dump")
        return buildJsonObject {
            put("foreground", device.foreground())
            put("count", elements.size)
            put("elements", JsonArray(elements))
        }
    }

    // ---- touch ----

    private suspend fun tap(args: JsonObject): JsonObject {
        val scale = args.scaleArg()
        val x = args.requiredNumber("x") / scale
        val y = args.requiredNumber("y") / scale
        device.gesture(listOf(listOf(Point(x.toFloat(), y.toFloat()))), TAP_MS)
        return buildJsonObject { put("x", Math.round(x)); put("y", Math.round(y)) }
    }

    private suspend fun swipe(args: JsonObject): JsonObject {
        val scale = args.scaleArg()
        val (x1, y1, x2, y2) = listOf("x1", "y1", "x2", "y2").map { args.requiredNumber(it) / scale }
        val ms = Math.round(args.numberArg("ms", 300.0))
        device.gesture(listOf(listOf(Point(x1.toFloat(), y1.toFloat()), Point(x2.toFloat(), y2.toFloat()))), maxOf(ms, 1))
        return buildJsonObject {
            put("x1", Math.round(x1)); put("y1", Math.round(y1)); put("x2", Math.round(x2)); put("y2", Math.round(y2)); put("ms", ms)
        }
    }

    private suspend fun gesture(args: JsonObject): JsonObject {
        val scale = args.scaleArg()
        val pointers = args["pointers"] as? JsonArray
        if (pointers == null || pointers.size !in 1..10) throw CommandError("invalid_args", "pointers must be a list of 1 to 10 paths")
        val paths = pointers.mapIndexed { i, path ->
            val points = (path as? JsonArray)?.map { pt ->
                val xy = (pt as? JsonArray)?.map { (it as? JsonPrimitive)?.takeIf { p -> !p.isString }?.content?.toDoubleOrNull() }
                if (xy == null || xy.size != 2 || xy.any { it == null || !it.isFinite() }) null
                else Point((xy[0]!! / scale).toFloat(), (xy[1]!! / scale).toFloat())
            }
            if (points.isNullOrEmpty() || points.any { it == null }) throw CommandError("invalid_args", "pointer $i must be a list of [x, y] points")
            points.filterNotNull()
        }
        val ms = Math.round(args.numberArg("ms", 300.0))
        device.gesture(paths, maxOf(ms, 1))
        return buildJsonObject { put("pointers", paths.size); put("ms", ms) }
    }

    // ---- type and keys ----

    private fun field(): Field = device.focusedField()
        ?: throw CommandError("no_text_field", "no text field has focus: tap one first (the app types into fields, not key by key)")

    /** Types at the cursor: through the input connection, else by splicing into the field's text. The path taken. */
    private fun insert(text: String): String {
        // Chrome's address bar drops whitespace that ends a longer commit (found on the phone), so it goes on its own.
        val body = text.trimEnd()
        val trailing = text.substring(body.length)
        if (body.isNotEmpty() && !device.commitText(body)) return splice(text)
        // Only what has not gone in yet falls back to the splice.
        if (trailing.isNotEmpty() && !device.commitText(trailing)) return splice(trailing)
        return "input_connection"
    }

    private fun splice(text: String): String {
        val r = TextEdit.insert(field(), text)
        device.setFieldText(r.text, r.cursor)
        return "set_text"
    }

    /** Runs a shell command (the shell bridge) for what the app cannot do; `instead` is the error without it. */
    private suspend fun shell(command: String, instead: CommandError, args: JsonObject): String {
        // Global input: the app in front is checked again once the bridge is connected, just before the command.
        val packages = args.ifForeground()
        val out = try {
            device.shell(command, if (packages == null) null else ({ requireForeground(packages) }))
        } catch (e: CommandError) {
            if (e.code != "shell_unavailable") throw e
            throw CommandError(instead.code, "${instead.detail} (the shell bridge would do it: ${e.detail})")
        }
        if (Regex("^(Error|Exception|java\\.)", RegexOption.MULTILINE).containsMatchIn(out)) {
            throw CommandError("device_error", out.trim().takeLast(300))
        }
        return out
    }

    /** Typing with no text field focused: `input text` through the shell bridge, ASCII only, as the adb client types. */
    private suspend fun shellType(text: String, method: String, args: JsonObject): JsonObject {
        val noField = CommandError("no_text_field", "no text field has focus: tap one first")
        if (method != "keys" || !ShellText.canType(text)) {
            throw CommandError("no_text_field", "no text field has focus: tap one first (with none, only ASCII can be typed, as keys)")
        }
        for (command in ShellText.commands(text)) shell(command, noField, args)
        return buildJsonObject {
            put("length", text.length)
            put("method", method)
            put("via", "shell")
            put("field_package", JsonNull)
            put("field_input_type", 0)
            put("password_field", false)
        }
    }

    private suspend fun type(args: JsonObject): JsonObject {
        val text = args.stringArg("text")!!
        val method = args.stringArg("method", required = false) ?: "keys"
        if (method != "keys" && method != "paste") throw CommandError("invalid_args", "method must be keys or paste")
        val first = device.focusedField() ?: return shellType(text, method, args)
        var via = "paste"
        if (method == "paste") {
            device.paste(text)
        } else {
            // A newline is a new line in a multi-line field, and the keyboard's action key (Go, Send) elsewhere.
            // Enter is pressed as a key: a new line in a multi-line field, the keyboard's action (Go, Send) in others.
            val lines = text.replace("\r\n", "\n").replace('\r', '\n').split('\n')
            lines.forEachIndexed { i, line ->
                if (line.isNotEmpty()) via = insert(line)
                if (i < lines.lastIndex && !device.sendKey("KEYCODE_ENTER")) {
                    if (field().multiLine) insert("\n") else device.imeEnter()
                }
            }
        }
        return buildJsonObject {
            put("length", text.length)
            put("method", method)
            put("via", via)
            put("field_package", first.pkg)
            put("field_input_type", first.inputType)
            put("password_field", first.password)
        }
    }

    private suspend fun key(args: JsonObject): JsonObject {
        val raw = args["key"] as? JsonPrimitive ?: throw CommandError("invalid_args", "key must be a name or a number")
        val name = if (!raw.isString && raw.intOrNull != null) {
            keyName(raw.intOrNull!!) ?: throw CommandError("invalid_args", "no key ${raw.content}")
        } else if (raw.isString && raw.content.matches(Regex("^\\d{1,4}$"))) {
            keyName(raw.content.toInt()) ?: throw CommandError("invalid_args", "no key ${raw.content}")
        } else Keys.name(raw.content)
        val action = Keys.action(name, args.boolArg("longpress", false))
        // A key for the focused field goes to it as a key press (a keyboard's), if the app has an input connection.
        if ((action.editsField || action is KeyAction.Unsupported) && device.sendKey(name)) {
            return buildJsonObject { put("key", raw) }
        }
        try {
            keyAction(action)
        } catch (e: CommandError) {
            // What the app cannot press (or has no field for) goes to the shell, as `input keyevent`.
            if (e.code != "unsupported" && e.code != "no_text_field") throw e
            if (!SHELL_KEY.matches(name)) throw e
            shell("input keyevent ${if (args.boolArg("longpress", false)) "--longpress " else ""}$name", e, args)
        }
        return buildJsonObject { put("key", raw) }
    }

    private fun keyAction(action: KeyAction) {
        when (action) {
            is KeyAction.Global -> device.globalAction(action.action)
            KeyAction.ImeEnter -> device.imeEnter()
            KeyAction.DeleteBack -> TextEdit.deleteBack(field())?.let { device.setFieldText(it.text, it.cursor) }
                ?: throw CommandError("unsupported", "the app cannot delete inside a password field; type its whole text again")
            KeyAction.DeleteForward -> TextEdit.deleteForward(field())?.let { device.setFieldText(it.text, it.cursor) }
                ?: throw CommandError("unsupported", "the app cannot delete inside a password field; type its whole text again")
            is KeyAction.Insert -> insert(action.text)
            KeyAction.CursorHome -> { field(); device.moveCursor(0) }
            KeyAction.CursorEnd -> device.moveCursor(field().text.length)
            is KeyAction.Scroll -> device.scroll(action.forward)
            is KeyAction.Volume -> device.adjustVolume(action.direction)
            is KeyAction.Media -> device.mediaKey(action.keyName)
            KeyAction.Wake -> device.wake()
            is KeyAction.Unsupported -> throw CommandError("unsupported", action.reason)
        }
    }

    // ---- apps ----

    private fun launch(args: JsonObject): JsonObject {
        val pkg = args.packageArg(required = true)!!
        device.launch(pkg)
        return buildJsonObject { put("package", pkg) }
    }

    private fun open(args: JsonObject): JsonObject {
        val url = args.stringArg("url")!!
        val pkg = args.packageArg()
        device.startIntent(IntentSpec(action = "android.intent.action.VIEW", data = url, pkg = pkg))
        return buildJsonObject { put("url", url); put("package", pkg) }
    }

    private suspend fun intent(args: JsonObject): JsonObject {
        val categories = when (val c = args["categories"]) {
            null, is JsonNull -> emptyList()
            is JsonArray -> c.map { (it as? JsonPrimitive)?.takeIf { p -> p.isString && p.content.isNotEmpty() }?.content
                ?: throw CommandError("invalid_args", "categories must be a list of strings") }
            else -> throw CommandError("invalid_args", "categories must be a list of strings")
        }
        val flags = args["flags"]?.takeIf { it !is JsonNull }?.let {
            (it as? JsonPrimitive)?.takeIf { p -> !p.isString }?.intOrNull?.takeIf { f -> f >= 0 }
                ?: throw CommandError("invalid_args", "flags must be a whole number")
        }
        val extras = when (val e = args["extras"]) {
            null, is JsonNull -> JsonObject(emptyMap())
            is JsonObject -> e.also { obj -> obj.forEach { (k, v) -> extraValue(k, v) } }
            else -> throw CommandError("invalid_args", "extras must be an object")
        }
        val spec = IntentSpec(
            action = args.stringArg("action", required = false),
            data = args.stringArg("data", required = false),
            type = args.stringArg("type", required = false),
            component = args.stringArg("component", required = false),
            pkg = args.packageArg(),
            categories = categories,
            flags = flags,
            extras = extras,
            broadcast = args.boolArg("broadcast", false),
        )
        if (listOf(spec.action, spec.data, spec.component, spec.pkg).all { it == null }) {
            throw CommandError("invalid_args", "give at least one of action, data, component or package")
        }
        if (args.boolArg("as_shell", false)) return shellIntent(spec, args)
        return buildJsonObject { put("output", device.startIntent(spec)) }
    }

    /**
     * The intent sent by the shell user (`am`, through the shell bridge), whose permissions (DUMP, ...)
     * the app lacks: only to a named component, so it is never a way to run anything else.
     */
    private suspend fun shellIntent(spec: IntentSpec, args: JsonObject): JsonObject {
        if (spec.component == null || !ShellCommands.COMPONENT_RE.matches(spec.component)) {
            throw CommandError("invalid_args", "as_shell needs component, e.g. com.example.app/.debug.Command")
        }
        val out = device.shell(ShellCommands.am(spec), guard(args))
        if (!spec.broadcast && Regex("^(Error|Exception|java\\.)", RegexOption.MULTILINE).containsMatchIn(out)) {
            throw CommandError("launch_failed", out.trim().takeLast(300))
        }
        return buildJsonObject { put("output", out.trim().takeLast(500)) }
    }

    /** The foreground check for a shell command, run once the bridge is up, just before it (null without if_foreground). */
    private fun guard(args: JsonObject): (() -> Unit)? = args.ifForeground()?.let { packages -> { requireForeground(packages) } }

    // ---- system ----

    private fun notifications(): JsonObject {
        val items = device.notifications()
        return buildJsonObject { put("count", items.size); put("notifications", JsonArray(items)) }
    }

    private fun statusbar(args: JsonObject): JsonObject {
        val action = args.stringArg("action")!!
        device.globalAction(
            when (action) {
                "notifications" -> GlobalAction.NOTIFICATIONS
                "settings" -> GlobalAction.QUICK_SETTINGS
                "collapse" -> GlobalAction.DISMISS_SHADE
                else -> throw CommandError("invalid_args", "action must be one of notifications, settings, collapse")
            },
        )
        return buildJsonObject { put("action", action) }
    }

    private fun rotation(args: JsonObject): JsonObject {
        val raw = args["rotation"] as? JsonPrimitive
        val rotation: Int? = when {
            raw != null && raw.isString && raw.content == "auto" -> null
            raw != null && !raw.isString && raw.intOrNull in 0..3 && raw.content.toIntOrNull() != null -> raw.intOrNull
            else -> throw CommandError("invalid_args", "rotation must be 0, 1, 2 or 3 (quarter turns) or \"auto\"")
        }
        val previous = device.rotation()
        device.setRotation(rotation)
        return buildJsonObject { put("rotation", raw!!); put("previous", rotationJson(previous)) }
    }

    private fun clipboardSet(args: JsonObject): JsonObject {
        val v = args["text"]
        if (v == null || v is JsonNull) {
            device.setClipboard(null)
            return buildJsonObject { put("length", 0); put("cleared", true) }
        }
        val text = (v as? JsonPrimitive)?.takeIf { it.isString }?.content
            ?: throw CommandError("invalid_args", "text must be a string, or null to empty the clipboard")
        device.setClipboard(text)
        return buildJsonObject { put("length", text.length) }
    }

    // ---- recording and audio ----

    private fun recordSeconds(args: JsonObject): Double {
        val seconds = args.numberArg("seconds", 10.0)
        if (seconds <= 0) throw CommandError("invalid_args", "seconds must be above 0")
        return seconds
    }

    private suspend fun recordStart(args: JsonObject): JsonObject {
        val seconds = recordSeconds(args)
        val size = args.stringArg("size", required = false)?.let {
            val m = Regex("^(\\d{2,5})x(\\d{2,5})$").matchEntire(it) ?: throw CommandError("invalid_args", "size must look like 720x1560")
            Size(m.groupValues[1].toInt(), m.groupValues[2].toInt())
        }
        val bitRate = args["bit_rate"]?.takeIf { it !is JsonNull }?.let {
            (it as? JsonPrimitive)?.takeIf { p -> !p.isString }?.intOrNull?.takeIf { b -> b > 0 }
                ?: throw CommandError("invalid_args", "bit_rate must be a whole number of bits per second")
        } ?: RECORD_BIT_RATE
        device.startRecording(seconds, size, bitRate)
        return buildJsonObject { put("recording", true); put("max_seconds", seconds) }
    }

    private suspend fun sendOwnFile(stream: Stream, path: String, mime: String, headers: Map<String, String>): Long = try {
        device.openRead(path).use { stream.sendFile(it, device.ownFileSize(path), mime, headers) }
    } finally {
        runCatching { device.deleteFile(path) }
    }

    private suspend fun recordStop(stream: Stream): JsonObject {
        val path = device.stopRecording() ?: throw CommandError("not_found", "no recording: start one with /record/start")
        val size = sendOwnFile(stream, path, "video/mp4", mapOf("X-Segments" to "1"))
        return buildJsonObject { put("segments", 1); put("bytes", size) }
    }

    private suspend fun record(args: JsonObject, stream: Stream): JsonObject {
        val started = recordStart(args)
        delay(Math.round((started["max_seconds"] as JsonPrimitive).content.toDouble() * 1000) + 300)
        return recordStop(stream)
    }

    private suspend fun audio(args: JsonObject, stream: Stream): JsonObject {
        val seconds = recordSeconds(args)
        val source = args.stringArg("source", required = false) ?: "output"
        if (source !in AUDIO_SOURCES) throw CommandError("invalid_args", "source must be one of ${AUDIO_SOURCES.joinToString(", ")}")
        val path = device.recordAudio(seconds, source)
        val size = sendOwnFile(stream, path, "audio/wav", mapOf("X-Audio-Source" to source))
        return buildJsonObject { put("seconds", seconds); put("source", source); put("bytes", size) }
    }

    // ---- wait and batch ----

    private suspend fun wait(args: JsonObject): JsonObject {
        val kind = args.stringArg("for")!!
        if (kind !in WAIT_KINDS) throw CommandError("invalid_args", "for must be one of ${WAIT_KINDS.joinToString(", ")}")
        val seconds = args.numberArg("seconds", 10.0)
        val interval = Math.round(args.numberArg("interval_ms", 500.0))
        val threshold = args.numberArg("threshold", 0.005)
        val pkg = args.packageArg()
        val wanted = if (kind == "text") args.stringArg("text") else null
        val started = System.nanoTime()
        fun elapsed() = (System.nanoTime() - started) / 1_000_000
        fun over() = elapsed() >= seconds * 1000
        fun result(met: Boolean, extra: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit = {}) =
            buildJsonObject { put("met", met); put("elapsed_ms", elapsed()); extra() }

        when (kind) {
            "text" -> while (true) {
                val found = UiTree.elements(device.uiRoots()).firstOrNull { UiTree.contains(it, wanted!!) }
                if (found != null) return result(true) { put("element", found) }
                if (over()) return result(false)
                pause(interval)
            }
            "foreground" -> {
                val first = device.foreground()
                while (true) {
                    val fg = device.foreground()
                    if (if (pkg != null) fg == pkg else fg != first) return result(true) { put("foreground", fg) }
                    if (over()) return result(false) { put("foreground", fg) }
                    pause(interval)
                }
            }
        }
        var reference = device.frame()
        var diff = 0.0
        while (true) {
            if (over()) return result(false) { put("diff", round4(diff)) }
            pause(interval)
            val frame = device.frame()
            diff = frameDiff(reference, frame)
            if ((kind == "change" && diff > threshold) || (kind == "stable" && diff <= threshold)) {
                return result(true) { put("diff", round4(diff)) }
            }
            if (kind == "stable") reference = frame
        }
    }

    /** Waits between polls; with no interval it still yields, so the deadline and a disconnect can stop the wait (delay(0) does not suspend). */
    private suspend fun pause(ms: Long) {
        if (ms > 0) delay(ms) else yield()
    }

    /** A batch's steps (a request's queued ones too), each a command a batch may run. */
    private fun steps(args: JsonObject): JsonArray {
        val steps = args["steps"] as? JsonArray
        if (steps.isNullOrEmpty()) throw CommandError("invalid_args", "steps must be a non-empty list")
        steps.forEachIndexed { i, step ->
            val cmd = ((step as? JsonObject)?.get("cmd") as? JsonPrimitive)?.content
            if (cmd !in BATCH_COMMANDS) throw CommandError("invalid_args", "step $i: cmd must be one of ${BATCH_COMMANDS.joinToString(", ")}")
            val a = (step as JsonObject)["args"]
            if (a != null && a !is JsonObject && a !is JsonNull) throw CommandError("invalid_args", "step $i: args must be an object")
        }
        return steps
    }

    private suspend fun batch(args: JsonObject): JsonObject {
        val steps = steps(args)
        val stopOnError = args.boolArg("stop_on_error", true)
        val inherited = args["if_foreground"]
        val results = mutableListOf<JsonObject>()
        for (step in steps) {
            val obj = step as JsonObject
            val cmd = (obj["cmd"] as JsonPrimitive).content
            val stepArgs = (obj["args"] as? JsonObject) ?: JsonObject(emptyMap())
            try {
                val result = if (cmd == "sleep") {
                    val ms = stepArgs.numberArg("ms", 0.0)
                    delay(Math.round(ms))
                    buildJsonObject { put("ms", ms) }
                } else {
                    val withGuard = if (inherited != null && !stepArgs.containsKey("if_foreground")) JsonObject(stepArgs + ("if_foreground" to inherited)) else stepArgs
                    handle(cmd, withGuard)
                }
                results += buildJsonObject { put("cmd", cmd); put("ok", true); put("result", result) }
            } catch (e: CommandError) {
                results += buildJsonObject { put("cmd", cmd); put("ok", false); put("error", e.code); put("detail", e.detail.take(300)) }
                if (stopOnError) break
            }
        }
        return buildJsonObject {
            put("results", JsonArray(results))
            put("completed", results.size)
            put("all_ok", results.size == steps.size && results.all { (it["ok"] as JsonPrimitive).content == "true" })
        }
    }

    // ---- asking the owner ----

    /**
     * Asks the owner whether the agent may use the phone (Device.ask), and on an Accept runs the queued
     * steps at once, as a batch (if_foreground applying to each step), before anything else can come
     * to the front. Every answer is a reply, not an error: accepted, denied or expired, with the
     * owner's message.
     */
    private suspend fun request(args: JsonObject): JsonObject {
        val reason = args.stringArg("reason")!!
        if (reason.isBlank()) throw CommandError("invalid_args", "reason must be a non-empty string")
        val seconds = args.numberArg("seconds", DEFAULT_REQUEST_S)
        if (seconds <= 0) throw CommandError("invalid_args", "seconds must be a number above 0")
        val queued = if (args["steps"] == null || args["steps"] is JsonNull) null else steps(args)
        val started = System.nanoTime()
        val answer = device.ask(reason, queued?.map { Ask.describe(it as JsonObject) }.orEmpty(), seconds)
        val waited = (System.nanoTime() - started) / 1_000_000
        return buildJsonObject {
            put("answer", when (answer.accepted) { true -> "accepted"; false -> "denied"; null -> "expired" })
            put("message", answer.message)
            put("waited_ms", waited)
            if (answer.accepted == true && queued != null) put("batch", batch(args))
        }
    }

    // ---- files ----

    private fun path(args: JsonObject): String {
        val p = args.stringArg("path")!!
        if (!p.startsWith("/")) throw CommandError("invalid_args", "path must be absolute, e.g. /sdcard/Download/file.pdf")
        return p
    }

    private fun entryJson(e: FileEntry) = buildJsonObject {
        put("name", e.name); put("type", e.type); put("size", e.size); put("modified", e.modified)
    }

    private fun files(args: JsonObject): JsonObject {
        val p = path(args)
        val entries = device.listFiles(p)
        return buildJsonObject { put("path", p); put("count", entries.size); put("entries", JsonArray(entries.map(::entryJson))) }
    }

    private suspend fun pull(args: JsonObject, stream: Stream): JsonObject {
        val p = path(args)
        val entry = device.stat(p)
        if (entry.type == "dir") throw CommandError("invalid_args", "$p is a directory; list it with /files")
        val mime = URLConnection.guessContentTypeFromName(p) ?: "application/octet-stream"
        val size = device.openRead(p).use { stream.sendFile(it, entry.size, mime, mapOf("X-File-Path" to p)) }
        return buildJsonObject { put("path", p); put("bytes", size) }
    }

    /**
     * The upload goes to a hidden .part file beside the destination, which replaces it only once the
     * whole file has arrived: a failed, cancelled or timed-out upload leaves the old file as it was.
     */
    private suspend fun push(args: JsonObject, stream: Stream): JsonObject {
        val p = path(args)
        val part = p.substringBeforeLast('/') + "/." + p.substringAfterLast('/') + ".part"
        val size = try {
            device.openWrite(part).use { stream.recvFile(it) }
        } catch (e: Throwable) { // a cancellation too
            runCatching { device.deleteFile(part) }
            throw e
        }
        try {
            device.moveFile(part, p)
        } catch (e: Throwable) {
            runCatching { device.deleteFile(part) }
            throw e
        }
        device.scanFile(p)
        return buildJsonObject { put("path", p); put("bytes", size) }
    }

    // ---- releases: installing, and what is installed; accessibility, logs, settings; an app's own files ----

    private fun packageName(args: JsonObject, name: String, what: String): String =
        args.packageArg(name) ?: throw CommandError("invalid_args", "$name must be $what")

    /**
     * Installs an APK already on the phone over the app that is there (`pm install -r`, through the
     * shell bridge): never with -d (a downgrade), and nothing here uninstalls or clears data. The
     * APK's own package must be the one named. Android's refusal (INSTALL_FAILED_...) is the error.
     * Installing Phone Relay itself ends the app before it can reply.
     */
    private suspend fun install(args: JsonObject): JsonObject {
        val p = path(args)
        val pkg = packageName(args, "package", "the APK's Android package name")
        val apk = device.apkInfo(p)
        if (apk.pkg != pkg) throw CommandError("package_mismatch", "$p is ${apk.pkg}, not $pkg")
        ShellCommands.parseInstall(device.shell(ShellCommands.install(p), guard(args)))?.let { throw CommandError("install_failed", it) }
        val installed = device.packageDetails(pkg)
        return buildJsonObject {
            put("package", pkg); put("version_name", installed.versionName); put("version_code", installed.versionCode)
        }
    }

    private fun strings(list: List<String>?) = list?.let { l -> JsonArray(l.map { JsonPrimitive(it) }) } ?: JsonNull

    private suspend fun accessibility(): JsonObject {
        val a = ShellCommands.parseAccessibility(device.shell(ShellCommands.ACCESSIBILITY))
        return buildJsonObject { put("enabled", strings(a.enabled)); put("bound", strings(a.bound)); put("missing", strings(a.missing)) }
    }

    private suspend fun logcat(args: JsonObject): JsonObject {
        val (command, lines) = ShellCommands.logcatCommand(args)
        val got = ShellCommands.logcatResult(device.shell(command), lines)
        return buildJsonObject { put("count", got.lines.size); put("truncated", got.truncated); put("lines", strings(got.lines)) }
    }

    private suspend fun settings(args: JsonObject): JsonObject {
        val (namespace, key) = ShellCommands.settingsCommand(args)
        val (value, night) = ShellCommands.parseSettings(device.shell(ShellCommands.settings(namespace, key)))
        return buildJsonObject { put("namespace", namespace); put("key", key); put("value", value); put("night_mode", night) }
    }

    private fun packageDetails(args: JsonObject): JsonObject {
        val d = device.packageDetails(packageName(args, "name", "an Android package name"))
        return buildJsonObject {
            put("name", d.name)
            put("version_name", d.versionName)
            put("version_code", d.versionCode)
            put("first_install_time", d.firstInstallTime)
            put("last_update_time", d.lastUpdateTime)
            put("debuggable", d.debuggable)
            put("enabled_state", d.enabledState)
            put("apk_path", d.apkPath)
            put("signing_sha256", strings(d.signingSha256.ifEmpty { null }))
        }
    }

    /** The installed APK (its base.apk), streamed as a pull is: Android lets any app read an installed APK. */
    private suspend fun packageApk(args: JsonObject, stream: Stream): JsonObject {
        val name = packageName(args, "name", "an Android package name")
        val d = device.packageDetails(name)
        val size = device.openRead(d.apkPath).use {
            stream.sendFile(it, device.stat(d.apkPath).size, APK_MIME, mapOf("X-Version-Code" to d.versionCode.toString(), "X-File-Path" to d.apkPath))
        }
        return buildJsonObject { put("name", name); put("path", d.apkPath); put("version_code", d.versionCode); put("bytes", size) }
    }

    /** run-as reads only a debuggable app's files: anything else is refused here first. */
    private fun debuggable(pkg: String) {
        if (!device.packageDetails(pkg).debuggable) throw CommandError("not_debuggable", "$pkg is not debuggable: run-as cannot read its files")
    }

    private suspend fun runAsFiles(args: JsonObject): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        val p = ShellCommands.dataPath(args, required = false) ?: "."
        debuggable(pkg)
        val out = device.shell(ShellCommands.runAsList(pkg, p))
        if (ShellCommands.MISSING_MARKER in out) throw CommandError("not_found", "$p does not exist in $pkg's data, or cannot be read")
        val entries = out.lines().mapNotNull(ShellCommands::parseStat)
        return buildJsonObject {
            put("package", pkg); put("path", p); put("count", entries.size); put("entries", JsonArray(entries.map(::entryJson)))
        }
    }

    /** A file in a debuggable app's own storage (`run-as PACKAGE cat PATH`), read-only, streamed as a pull is. */
    private suspend fun runAs(args: JsonObject, stream: Stream): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        val p = ShellCommands.dataPath(args)!!
        debuggable(pkg)
        val entry = device.shell(ShellCommands.runAsStat(pkg, p)).lines().firstNotNullOfOrNull(ShellCommands::parseStat)
            ?: throw CommandError("not_found", "$p does not exist in $pkg's data, or cannot be read")
        if (entry.type != "file") throw CommandError("invalid_args", "$p is a ${entry.type}; list a folder with /run-as/files")
        val out = device.shellToFile(ShellCommands.runAsCat(pkg, p))
        try {
            // run-as's own errors arrive as if they were the file: a size other than stat's is one
            val got = device.ownFileSize(out)
            if (got != entry.size) throw CommandError("device_error", "read $got bytes of ${entry.size}")
            val mime = URLConnection.guessContentTypeFromName(p) ?: "application/octet-stream"
            val size = device.openRead(out).use { stream.sendFile(it, got, mime, mapOf("X-File-Path" to p, "X-Package" to pkg)) }
            return buildJsonObject { put("package", pkg); put("path", p); put("bytes", size) }
        } finally {
            runCatching { device.deleteFile(out) }
        }
    }

    // ---- what Decrastination's workflow does over adb: a provider's rows; writing to, archiving and pausing a debuggable app; dark mode; force-stop ----

    /** A provider's rows (`content query`, read-only): the calendar's events, say. */
    private suspend fun content(args: JsonObject): JsonObject {
        val (command, projection) = ShellCommands.contentCommand(args)
        val rows = ShellCommands.parseContent(device.shell(command), projection)
        return buildJsonObject {
            put("count", rows.size)
            put("rows", JsonArray(rows.map { row -> buildJsonObject { row.forEach { (k, v) -> put(k, v) } } }))
        }
    }

    /** A folder of a debuggable app's own storage as a tar (`run-as PACKAGE tar -cf - PATH`), read-only. */
    private suspend fun runAsArchive(args: JsonObject, stream: Stream): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        val p = ShellCommands.dataPath(args, required = false) ?: "."
        debuggable(pkg)
        val out = device.shellToFile(ShellCommands.runAsTar(pkg, p))
        try {
            val size = device.ownFileSize(out)
            val head = device.openRead(out).use { it.readNBytes(512) }
            val tail = device.openRead(out).use { it.skip(maxOf(0L, size - 64)); it.readBytes() }
            val failed = String(tail, Charsets.ISO_8859_1).trimEnd().endsWith(ShellCommands.TAR_FAILED_MARKER)
            if (!ShellCommands.isTar(head)) {
                val detail = if (failed) "it does not exist or cannot be read" else String(head).trim().takeLast(200)
                throw CommandError("not_found", "$p could not be archived: $detail")
            }
            // A whole archive's header, but tar left out something it could not read
            if (failed) throw CommandError("device_error", "tar could not read all of $p: the archive would be incomplete")
            val sent = device.openRead(out).use { stream.sendFile(it, size, "application/x-tar", mapOf("X-Package" to pkg, "X-File-Path" to p)) }
            return buildJsonObject { put("package", pkg); put("path", p); put("bytes", sent) }
        } finally {
            runCatching { device.deleteFile(out) }
        }
    }

    /**
     * Writes a file into a debuggable app's own storage: to a hidden .part beside it (through the
     * shell bridge, as `adb exec-in run-as`), its size checked, then moved over the file, so a
     * failed upload leaves the old file as it was.
     */
    private suspend fun runAsPush(args: JsonObject, stream: Stream): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        val p = ShellCommands.dataPath(args, file = true)!!
        debuggable(pkg)
        // mv would put the file inside a folder of that name: refused before anything is sent
        val existing = device.shell(ShellCommands.runAsStat(pkg, p)).lines().firstNotNullOfOrNull(ShellCommands::parseStat)
        if (existing?.type == "dir") throw CommandError("invalid_args", "$p is a folder; name the file to write")
        // The upload goes to a file of the app's own first, never whole into memory
        val upload = device.newOwnFile()
        try {
            val size = device.openWrite(upload).use { stream.recvFile(it) }
            val (folder, part) = ShellCommands.partPath(p)
            device.shellWithInput(ShellCommands.runAsWrite(pkg, folder, part), upload)
            // The input's end reaches cat once adbd has written it: wait a little for the whole file.
            var written = ""
            for (attempt in 0 until WRITE_CHECKS) {
                written = device.shell(ShellCommands.runAsSize(pkg, part)).trim()
                if (written == size.toString()) break
                delay(WRITE_CHECK_MS)
            }
            if (written != size.toString()) {
                runCatching { device.shell(ShellCommands.runAsDelete(pkg, part)) }
                throw CommandError("device_error", "wrote ${written.take(100)} bytes of $size")
            }
            // With if_foreground, the file is replaced only if that app is still in front (the upload took a while)
            val moved = try {
                device.shell(ShellCommands.runAsMove(pkg, part, p), guard(args))
            } catch (e: CommandError) {
                runCatching { device.shell(ShellCommands.runAsDelete(pkg, part)) }
                throw e
            }
            moved.trim().takeIf { it.isNotEmpty() }?.let { throw CommandError("device_error", it.takeLast(300)) }
            return buildJsonObject { put("package", pkg); put("path", p); put("bytes", size) }
        } finally {
            runCatching { device.deleteFile(upload) }
        }
    }

    /** Deletes one file (never a folder) in a debuggable app's own storage. */
    private suspend fun runAsDelete(args: JsonObject): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        val p = ShellCommands.dataPath(args, file = true)!!
        debuggable(pkg)
        val entry = device.shell(ShellCommands.runAsStat(pkg, p)).lines().firstNotNullOfOrNull(ShellCommands::parseStat)
            ?: throw CommandError("not_found", "$p does not exist in $pkg's data, or cannot be read")
        if (entry.type != "file") throw CommandError("invalid_args", "$p is a ${entry.type}; only a file is deleted")
        // With if_foreground, only if that app is still in front, checked just before the delete
        device.shell(ShellCommands.runAsDelete(pkg, p), guard(args)).trim().takeIf { it.isNotEmpty() }?.let { throw CommandError("device_error", it.takeLast(300)) }
        return buildJsonObject { put("package", pkg); put("path", p); put("deleted", true) }
    }

    /**
     * STOP, CONT or KILL to a debuggable app's main process, as the app's own user (run-as): to pause
     * it while a store is edited by hand, and end it after (an app with a bound accessibility service
     * is started again by Android, still enabled, unlike a force-stop).
     */
    private suspend fun runAsKill(args: JsonObject): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        val signal = args.stringArg("signal")!!
        if (signal !in ShellCommands.SIGNALS) throw CommandError("invalid_args", "signal must be one of ${ShellCommands.SIGNALS.joinToString(", ")}")
        debuggable(pkg)
        val pid = device.shell("pidof $pkg; true").trim().split(Regex("\\s+")).firstOrNull()?.toIntOrNull()
            ?: throw CommandError("not_found", "$pkg is not running")
        // With if_foreground, only if that app is still in front, checked just before the signal
        device.shell("run-as $pkg kill -$signal $pid 2>&1", guard(args)).trim().takeIf { it.isNotEmpty() }?.let { throw CommandError("device_error", it.takeLast(300)) }
        return buildJsonObject { put("package", pkg); put("pid", pid); put("signal", signal) }
    }

    /** Dark mode on (yes), off (no) or automatic; the reply's `previous` is the old setting, to put back. */
    private suspend fun nightMode(args: JsonObject): JsonObject {
        val mode = args.stringArg("mode")!!
        if (mode !in ShellCommands.NIGHT_MODES) throw CommandError("invalid_args", "mode must be one of ${ShellCommands.NIGHT_MODES.joinToString(", ")}")
        val previous = ShellCommands.nightMode(device.shell("cmd uimode night"))
        device.shell("cmd uimode night $mode", guard(args))
        return buildJsonObject { put("night_mode", ShellCommands.nightMode(device.shell("cmd uimode night"))); put("previous", previous) }
    }

    /**
     * Force-stops an app a task opened (`am force-stop`, through the shell bridge). Refused for an app
     * with an enabled accessibility service, which Android would drop from the enabled list, and for
     * the system's own.
     */
    private suspend fun forceStop(args: JsonObject): JsonObject {
        val pkg = packageName(args, "package", "an Android package name")
        if (pkg in ShellCommands.STOP_REFUSED) throw CommandError("stop_refused", "$pkg is never force-stopped")
        val enabled = ShellCommands.parseAccessibility(device.shell("settings get secure enabled_accessibility_services")).enabled
        if (enabled.any { it.substringBefore('/') == pkg }) {
            throw CommandError("stop_refused", "$pkg has an enabled accessibility service, which a force-stop would turn off")
        }
        device.packageDetails(pkg)
        device.shell("am force-stop $pkg", guard(args))
        return buildJsonObject { put("package", pkg) }
    }

    companion object {
        const val TAP_MS = 50L
        /** How often, and how far apart, a write into an app's storage is checked for its whole size. */
        const val WRITE_CHECKS = 25
        const val WRITE_CHECK_MS = 200L
        const val APK_MIME = "application/vnd.android.package-archive"
        const val RECORD_BIT_RATE = 2_000_000
        val AUDIO_SOURCES = listOf("output", "mic")
        val WAIT_KINDS = listOf("change", "stable", "foreground", "text")
        val BATCH_COMMANDS = listOf(
            "state", "screenshot", "ui", "tap", "swipe", "type", "key", "launch", "open", "intent", "notifications",
            "statusbar", "rotation", "wait", "files", "gesture", "clipboard_set", "clipboard_get", "record_start",
            "chrome_tabs", "chrome_eval", "chrome_cdp", "chrome_new", "chrome_close", "chrome_activate", "install",
            "accessibility", "logcat", "package", "settings", "run_as_files", "content", "run_as_delete", "run_as_kill",
            "night_mode", "force_stop", "sleep",
        )
        /** A key the shell may be given: a KEYCODE_* name or a number. */
        private val SHELL_KEY = Regex("^(KEYCODE_[A-Z0-9_]{1,40}|\\d{1,4})$")

        /** Fraction of pixels that changed noticeably between two frames (1 for different sizes). */
        fun frameDiff(a: Frame, b: Frame): Double {
            if (a.width != b.width || a.height != b.height || a.grey.size != b.grey.size || a.grey.isEmpty()) return 1.0
            var changed = 0
            for (i in a.grey.indices) {
                if (Math.abs((a.grey[i].toInt() and 0xFF) - (b.grey[i].toInt() and 0xFF)) > 24) changed++
            }
            return changed.toDouble() / a.grey.size
        }

        private fun round4(v: Double) = Math.round(v * 10000) / 10000.0
    }
}
