package com.thomaswcode.phonerelay.commands

import java.net.URLConnection
import java.util.Base64
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
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
 * Differences from the adb client: if_foreground is checked just before the action, not in the
 * same shell command, so a focus change in between is still possible; typing needs a focused text
 * field, and goes through the accessibility service's own input connection to it (as a keyboard);
 * keys are limited to what an app can do (Keys), any key reaching a focused field; the shell-only
 * commands (reading the clipboard, recording, audio, Chrome DevTools) are refused as unsupported.
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
        "wait" to { a, _ -> wait(a) },
        "batch" to { a, _ -> batch(a) },
        "files" to { a, _ -> files(a) },
        "pull" to { a, s -> pull(a, s ?: noStream("pull")) },
        "push" to { a, s -> push(a, s ?: noStream("push")) },
    )

    /** The commands this client runs, announced to the relay in hello and in /state. */
    val capabilities: List<String> = handlers.keys.sorted()

    suspend fun handle(cmd: String, args: JsonObject, stream: Stream? = null): JsonObject {
        val handler = handlers[cmd] ?: if (cmd in ADB_ONLY) {
            throw CommandError("unsupported", "$cmd needs the adb client (phone/phone_client.py); the Android app cannot do it")
        } else throw CommandError("invalid_args", "unknown command: $cmd")
        val packages = args.ifForeground()
        val timeout = args.timeoutArg(defaultTimeout(cmd, args))
        // Checked before the command starts; a batch checks each step instead.
        if (packages != null && cmd != "batch") requireForeground(packages)
        // withTimeoutOrNull: only this deadline turns into "timeout"; an outer one cancels as usual.
        return withTimeoutOrNull((timeout * 1000).toLong()) { handler(args, stream) }
            ?: throw CommandError("timeout", "$cmd took longer than ${fmt(timeout)} s; pass a larger timeout")
    }

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
        val engine = args.stringArg("engine", required = false) ?: "helper"
        if (engine == "uiautomator") throw CommandError("unsupported", "engine=uiautomator needs the adb client")
        if (engine != "helper") throw CommandError("invalid_args", "engine must be helper or uiautomator")
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

    private fun type(args: JsonObject): JsonObject {
        val text = args.stringArg("text")!!
        val method = args.stringArg("method", required = false) ?: "keys"
        if (method != "keys" && method != "paste") throw CommandError("invalid_args", "method must be keys or paste")
        val first = field()
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

    private fun key(args: JsonObject): JsonObject {
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
        return buildJsonObject { put("key", raw) }
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

    private fun intent(args: JsonObject): JsonObject {
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
        return buildJsonObject { put("output", device.startIntent(spec)) }
    }

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
                delay(interval)
            }
            "foreground" -> {
                val first = device.foreground()
                while (true) {
                    val fg = device.foreground()
                    if (if (pkg != null) fg == pkg else fg != first) return result(true) { put("foreground", fg) }
                    if (over()) return result(false) { put("foreground", fg) }
                    delay(interval)
                }
            }
        }
        var reference = device.frame()
        var diff = 0.0
        while (true) {
            if (over()) return result(false) { put("diff", round4(diff)) }
            delay(interval)
            val frame = device.frame()
            diff = frameDiff(reference, frame)
            if ((kind == "change" && diff > threshold) || (kind == "stable" && diff <= threshold)) {
                return result(true) { put("diff", round4(diff)) }
            }
            if (kind == "stable") reference = frame
        }
    }

    private suspend fun batch(args: JsonObject): JsonObject {
        val steps = args["steps"] as? JsonArray
        if (steps.isNullOrEmpty()) throw CommandError("invalid_args", "steps must be a non-empty list")
        steps.forEachIndexed { i, step ->
            val cmd = ((step as? JsonObject)?.get("cmd") as? JsonPrimitive)?.content
            if (cmd !in BATCH_COMMANDS) throw CommandError("invalid_args", "step $i: cmd must be one of ${BATCH_COMMANDS.joinToString(", ")}")
            val a = (step as JsonObject)["args"]
            if (a != null && a !is JsonObject && a !is JsonNull) throw CommandError("invalid_args", "step $i: args must be an object")
        }
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

    private suspend fun push(args: JsonObject, stream: Stream): JsonObject {
        val p = path(args)
        val size = device.openWrite(p).use { stream.recvFile(it) }
        device.scanFile(p)
        return buildJsonObject { put("path", p); put("bytes", size) }
    }

    companion object {
        const val TAP_MS = 50L
        val WAIT_KINDS = listOf("change", "stable", "foreground", "text")
        val BATCH_COMMANDS = listOf(
            "state", "screenshot", "ui", "tap", "swipe", "type", "key", "launch", "open", "intent", "notifications",
            "statusbar", "rotation", "wait", "files", "gesture", "clipboard_set", "sleep",
        )
        /** The adb client's commands that need the shell user's privileges. */
        val ADB_ONLY = setOf(
            "clipboard_get", "record", "record_start", "record_stop", "audio", "chrome_sockets", "chrome_tabs",
            "chrome_eval", "chrome_cdp", "chrome_new", "chrome_close", "chrome_activate",
        )

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
