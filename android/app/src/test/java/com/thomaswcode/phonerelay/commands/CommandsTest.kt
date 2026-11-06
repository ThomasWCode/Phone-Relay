package com.thomaswcode.phonerelay.commands

import java.util.Base64
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.yield
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Test

class CommandsTest {
    private val device = FakeDevice()
    private val commands = Commands(device, "1.0.0") { code -> mapOf(4 to "KEYCODE_BACK", 26 to "KEYCODE_POWER")[code] }

    private fun args(json: String) = Json.parseToJsonElement(json) as JsonObject
    private suspend fun run(cmd: String, json: String = "{}") = commands.handle(cmd, args(json))
    private suspend fun error(cmd: String, json: String = "{}"): CommandError = assertFailsWith<CommandError> { run(cmd, json) }
    private fun JsonObject.str(k: String) = this[k]!!.jsonPrimitive.content

    @Test
    fun state() = runTest {
        val st = run("state")
        assertEquals("com.android.chrome", st.str("foreground"))
        assertEquals("android-app", st.str("client"))
        assertEquals(87, st["battery"]!!.jsonPrimitive.int)
        assertTrue(st["capabilities"]!!.jsonArray.map { it.jsonPrimitive.content }.containsAll(listOf("tap", "type", "pull")))
        assertTrue("chrome_eval" !in st["capabilities"]!!.jsonArray.map { it.jsonPrimitive.content })
    }

    @Test
    fun screenshotAndUi() = runTest {
        val shot = run("screenshot", """{"scale": 2}""")
        assertEquals(1080, shot["width"]!!.jsonPrimitive.int) // no upscaling
        assertTrue(Base64.getDecoder().decode(shot.str("data"))[0] == 0xFF.toByte())
        val ui = run("ui")
        val texts = ui["elements"]!!.jsonArray.map { it.jsonObject.str("text") }
        assertEquals(listOf("Hello & welcome", ""), texts) // the invisible node and its child are dropped
        assertEquals("url_bar", ui["elements"]!!.jsonArray[1].jsonObject.str("id"))
        assertEquals("unsupported", error("ui", """{"engine": "uiautomator"}""").code)
    }

    @Test
    fun tapsSwipesAndGestures() = runTest {
        run("tap", """{"x": 270, "y": 585, "scale": 0.5}""")
        assertEquals(listOf(listOf(Point(540f, 1170f))), device.gestures.last().first)
        run("swipe", """{"x1": 1, "y1": 2, "x2": 3, "y2": 4, "ms": 0}""")
        assertEquals(1L, device.gestures.last().second) // Android needs at least 1 ms
        run("gesture", """{"pointers": [[[500, 1000], [300, 800]], [[580, 1080], [780, 1280]]], "ms": 400}""")
        assertEquals(2, device.gestures.last().first.size)
        for (bad in listOf("""{}""", """{"pointers": []}""", """{"pointers": [[]]}""", """{"pointers": [[[1]]]}""", """{"pointers": [[["1", 2]]]}""")) {
            assertEquals("invalid_args", error("gesture", bad).code, bad)
        }
        assertEquals("invalid_args", error("tap", """{"x": "1", "y": 2}""").code)
    }

    @Test
    fun typingSplicesAtTheCursor() = runTest {
        device.field = device.field!!.copy(text = "hello world", selectionStart = 5, selectionEnd = 5)
        val res = run("type", """{"text": ", dear"}""")
        assertEquals("hello, dear world", device.field!!.text)
        assertEquals(11, device.field!!.selectionStart)
        assertEquals("keys", res.str("method"))
        run("type", """{"text": "日本 😀"}""")
        assertEquals("hello, dear日本 😀 world", device.field!!.text)
    }

    @Test
    fun newlinesSubmitInOneLineFieldsAndBreakInMultiLineOnes() = runTest {
        device.field = device.field!!.copy(text = "", selectionStart = 0, selectionEnd = 0)
        run("type", """{"text": "query\n"}""")
        assertEquals(listOf("query"), device.imeEnters)
        device.field = device.field!!.copy(text = "", multiLine = true, selectionStart = 0, selectionEnd = 0)
        run("type", """{"text": "one\ntwo"}""")
        assertEquals("one\ntwo", device.field!!.text)
    }

    @Test
    fun withAnInputConnectionTextAndKeysGoInAsAKeyboardWould() = runTest {
        device.ime = true
        run("type", """{"text": "relay test 50% \nnext"}""")
        // the trailing space kept, as its own commit (Chrome's address bar drops it from a longer one)
        assertEquals(listOf("relay test 50%", " ", "next"), device.committed)
        assertEquals(listOf("KEYCODE_ENTER"), device.keysSent)
        run("key", """{"key": "delete"}""")
        run("key", """{"key": "KEYCODE_MOVE_END"}""")
        run("key", """{"key": "KEYCODE_CAMERA"}""") // with a field focused, any key goes to it
        assertEquals(listOf("KEYCODE_ENTER", "KEYCODE_DEL", "KEYCODE_MOVE_END", "KEYCODE_CAMERA"), device.keysSent)
        run("key", """{"key": "back"}""") // not a field key: still the global action
        assertEquals(listOf(GlobalAction.BACK), device.globals)
        device.ime = false
        assertEquals("unsupported", error("key", """{"key": "KEYCODE_CAMERA"}""").code) // no input connection
    }

    @Test
    fun pasteAndNoField() = runTest {
        run("type", """{"text": "pasted ✓", "method": "paste"}""")
        assertEquals(listOf("pasted ✓"), device.pasted)
        device.field = null
        assertEquals("no_text_field", error("type", """{"text": "x"}""").code)
        assertEquals("invalid_args", error("type", """{"text": "x", "method": "voice"}""").code)
    }

    @Test
    fun keys() = runTest {
        run("key", """{"key": "back"}""")
        run("key", """{"key": 26}""")
        run("key", """{"key": "KEYCODE_POWER", "longpress": true}""")
        run("key", """{"key": "KEYCODE_DPAD_UP"}""")
        assertEquals(listOf(GlobalAction.BACK, GlobalAction.LOCK_SCREEN, GlobalAction.POWER_DIALOG, GlobalAction.DPAD_UP), device.globals)
        run("key", """{"key": "KEYCODE_VOLUME_DOWN"}""")
        run("key", """{"key": "KEYCODE_MEDIA_PLAY_PAUSE"}""")
        run("key", """{"key": "KEYCODE_PAGE_DOWN"}""")
        assertEquals(listOf(-1), device.volume)
        assertEquals(listOf("KEYCODE_MEDIA_PLAY_PAUSE"), device.media)
        assertEquals(listOf(true), device.scrolls)
        device.field = device.field!!.copy(text = "abc", selectionStart = 3, selectionEnd = 3)
        run("key", """{"key": "delete"}""")
        run("key", """{"key": "KEYCODE_X"}""")
        assertEquals("abx", device.field!!.text)
        run("key", """{"key": "enter"}""")
        assertEquals(listOf("abx"), device.imeEnters)
        val e = error("key", """{"key": "KEYCODE_CAMERA"}""")
        assertEquals("unsupported", e.code)
        for (bad in listOf("""{"key": "power"}""", """{"key": "KEYCODE_BACK; x"}""", """{"key": 999}""", """{}""")) {
            assertEquals("invalid_args", error("key", bad).code, bad)
        }
    }

    @Test
    fun appsAndIntents() = runTest {
        run("launch", """{"package": "com.android.settings"}""")
        assertEquals(listOf("com.android.settings"), device.launched)
        assertEquals("launch_failed", error("launch", """{"package": "com.example.missing"}""").code)
        run("open", """{"url": "mailto:someone@example.com?subject=Hi", "package": "com.google.android.gm"}""")
        assertEquals(IntentSpec(action = "android.intent.action.VIEW", data = "mailto:someone@example.com?subject=Hi", pkg = "com.google.android.gm"), device.intents.last())
        assertEquals("launch_failed", error("open", """{"url": "nosuchscheme:x"}""").code)
        run("intent", """{"action": "android.intent.action.SEND", "type": "text/plain", "categories": ["c"], "flags": 1,
            "extras": {"t": "x", "n": 5, "big": 1099511627776, "f": 1.5, "b": true, "z": null}, "broadcast": true}""")
        val spec = device.intents.last()
        assertTrue(spec.broadcast)
        assertEquals(listOf("c"), spec.categories)
        assertEquals(1099511627776L, extraValue("big", spec.extras["big"]!!))
        assertEquals(5, extraValue("n", spec.extras["n"]!!))
        for (bad in listOf("""{}""", """{"type": "x"}""", """{"action": "a", "extras": {"k": [1]}}""", """{"action": "a", "flags": -1}""",
            """{"action": "a", "categories": "c"}""", """{"action": "a", "broadcast": "yes"}""")) {
            assertEquals("invalid_args", error("intent", bad).code, bad)
        }
    }

    @Test
    fun systemCommands() = runTest {
        run("statusbar", """{"action": "collapse"}""")
        assertEquals(GlobalAction.DISMISS_SHADE, device.globals.last())
        val r = run("rotation", """{"rotation": 1}""")
        assertEquals(JsonPrimitive(true), r["previous"]!!.jsonObject["auto"])
        assertEquals(Rotation(false, 1), device.rotationNow)
        for (bad in listOf("4", "-1", "true", "\"left\"", "1.5")) assertEquals("invalid_args", error("rotation", """{"rotation": $bad}""").code, bad)
        device.canWriteSettings = false
        assertEquals("permission_missing", error("rotation", """{"rotation": "auto"}""").code)
        assertEquals(1, run("notifications")["count"]!!.jsonPrimitive.int)
        device.notificationAccess = false
        assertEquals("permission_missing", error("notifications").code)
        run("clipboard_set", """{"text": "x ✓"}""")
        assertEquals("x ✓", device.clip)
        run("clipboard_set", """{"text": null}""")
        assertEquals(null, device.clip)
    }

    @Test
    fun ifForegroundIsCheckedBeforeEveryCommand() = runTest {
        device.fg = "com.android.settings"
        for (cmd in listOf("tap" to """{"x": 1, "y": 1}""", "state" to "{}", "launch" to """{"package": "com.a.b"}""")) {
            val e = error(cmd.first, cmd.second.dropLast(1).let { if (it.length > 1) "$it, " else it } + """"if_foreground": "com.android.chrome"}""")
            assertEquals("foreground_mismatch", e.code, cmd.first)
        }
        assertTrue(device.gestures.isEmpty())
        run("tap", """{"x": 1, "y": 1, "if_foreground": ["com.android.settings"]}""")
        device.isAwake = false
        assertEquals("foreground_mismatch", error("tap", """{"x": 1, "y": 1, "if_foreground": "com.android.settings"}""").code)
        assertEquals("invalid_args", error("tap", """{"x": 1, "y": 1, "if_foreground": "a b"}""").code)
    }

    @Test
    fun adbOnlyCommandsAreUnsupported() = runTest {
        for (cmd in listOf("clipboard_get", "record_start", "audio", "chrome_eval")) assertEquals("unsupported", error(cmd).code, cmd)
        assertEquals("invalid_args", error("shell").code)
    }

    @Test
    fun timeouts() = runTest {
        device.gestureDelayMs = 5_000
        assertEquals("timeout", error("tap", """{"x": 1, "y": 1, "timeout": 1}""").code)
        device.gestureDelayMs = 500
        run("tap", """{"x": 1, "y": 1, "timeout": 86400}""")
        assertEquals("invalid_args", error("tap", """{"x": 1, "y": 1, "timeout": 0}""").code)
        assertEquals(130.0, defaultTimeout("type", args("""{"text": "${"x".repeat(1000)}"}""")))
        assertEquals(30.0 + 30 + 2 + 5, defaultTimeout("batch", args("""{"steps": [{"cmd": "tap", "args": {}}, {"cmd": "sleep", "args": {"ms": 2000}}, {"cmd": "key", "args": {"timeout": 5}}]}""")))
    }

    @Test
    fun waits() = runBlocking { // real time: /wait measures elapsed time with the real clock
        val res = run("wait", """{"for": "text", "text": "welcome", "interval_ms": 0}""")
        assertEquals(JsonPrimitive(true), res["met"])
        assertEquals("Hello & welcome", res["element"]!!.jsonObject.str("text"))
        assertEquals(JsonPrimitive(false), run("wait", """{"for": "change", "seconds": 0.05, "interval_ms": 10}""")["met"])
        assertEquals(JsonPrimitive(true), run("wait", """{"for": "stable", "interval_ms": 0}""")["met"])
        assertEquals(JsonPrimitive(true), run("wait", """{"for": "foreground", "package": "com.android.chrome"}""")["met"])
        assertEquals("invalid_args", error("wait", """{"for": "nothing"}""").code)
    }

    @Test
    fun waitForAChangeSeesIt() = runBlocking {
        val waiting = async { run("wait", """{"for": "change", "interval_ms": 1, "seconds": 5}""") }
        yield()
        device.shade = 220
        assertEquals(JsonPrimitive(true), waiting.await()["met"])
    }

    @Test
    fun batch() = runTest {
        val res = run("batch", """{"steps": [{"cmd": "tap", "args": {"x": 1, "y": 2}}, {"cmd": "sleep", "args": {"ms": 10}},
            {"cmd": "launch", "args": {"package": "com.example.missing"}}, {"cmd": "tap", "args": {"x": 3, "y": 4}}]}""")
        assertEquals(3, res["completed"]!!.jsonPrimitive.int)
        assertEquals(JsonPrimitive(false), res["all_ok"])
        assertEquals("launch_failed", res["results"]!!.jsonArray[2].jsonObject.str("error"))
        assertEquals(1, device.gestures.size)
        device.fg = "com.android.settings"
        val guarded = run("batch", """{"if_foreground": "com.android.chrome", "stop_on_error": false, "steps": [
            {"cmd": "tap", "args": {"x": 1, "y": 1}}, {"cmd": "tap", "args": {"x": 2, "y": 2, "if_foreground": "com.android.settings"}}]}""")
        assertEquals(listOf("false", "true"), guarded["results"]!!.jsonArray.map { it.jsonObject.str("ok") })
        for (bad in listOf("""{"steps": []}""", """{"steps": [{"cmd": "pull"}]}""", """{"steps": [{"cmd": "tap", "args": 5}]}""")) {
            assertEquals("invalid_args", error("batch", bad).code, bad)
        }
    }

    @Test
    fun filesPullAndPush() = runTest {
        val listing = run("files", """{"path": "/sdcard/Download"}""")
        assertEquals("a.txt", listing["entries"]!!.jsonArray[0].jsonObject.str("name"))
        assertEquals("not_found", error("files", """{"path": "/sdcard/nope"}""").code)
        assertEquals("invalid_args", error("files", """{"path": "sdcard"}""").code)

        val sent = mutableListOf<JsonObject>()
        val pullStream = Stream("p") { msg -> sent += msg; if (msg.str("type") == "chunk") { /* acked below */ } }
        val pulled = commands.handle("pull", args("""{"path": "/sdcard/Download/a.txt"}"""), pullStream)
        assertEquals(10, pulled["bytes"]!!.jsonPrimitive.int)
        assertEquals("text/plain", sent.first().str("mime"))
        assertEquals("hello file", String(Base64.getDecoder().decode(sent.first { it.str("type") == "chunk" }.str("data"))))
        assertEquals("invalid_args", assertFailsWith<CommandError> { commands.handle("pull", args("""{"path": "/sdcard/Download"}"""), pullStream) }.code)

        val acks = mutableListOf<JsonObject>()
        val pushStream = Stream("u") { acks += it }
        val data = "uploaded ✓".toByteArray()
        pushStream.inbox.trySend(args("""{"type": "chunk", "seq": 0, "data": "${Base64.getEncoder().encodeToString(data)}"}"""))
        pushStream.inbox.trySend(args("""{"type": "end"}"""))
        commands.handle("push", args("""{"path": "/sdcard/Download/new.txt"}"""), pushStream)
        assertEquals("uploaded ✓", String(device.written["/sdcard/Download/new.txt"]!!))
        assertEquals(listOf("/sdcard/Download/new.txt"), device.scanned)
        assertEquals(1, acks.size)
        assertEquals("invalid_args", error("pull", """{"path": "/sdcard/Download/a.txt"}""").code) // no stream: not over the relay
    }
}

class StreamTest {
    @Test
    fun sendFileKeepsToTheWindowAndStopsOnCancel() = runTest { supervisorScope {
        val sent = Channel<JsonObject>(Channel.UNLIMITED)
        val stream = Stream("d") { sent.send(it) }
        val data = ByteArray(Stream.CHUNK * 20 + 7) { it.toByte() }
        val job = async { stream.sendFile(data.inputStream(), data.size.toLong(), "application/octet-stream", emptyMap()) }
        val start = sent.receive()
        assertEquals("start", (start["type"] as JsonPrimitive).content)
        val chunks = mutableListOf<JsonObject>()
        repeat(Stream.WINDOW) { chunks += sent.receive() }
        yield()
        assertTrue(sent.tryReceive().isFailure) // the window is full: nothing more until an ack
        stream.inbox.send(Json.parseToJsonElement("""{"type": "ack", "seq": 0}""") as JsonObject)
        chunks += sent.receive()
        stream.inbox.send(Json.parseToJsonElement("""{"type": "cancel"}""") as JsonObject)
        assertEquals("cancelled", assertFailsWith<CommandError> { job.await() }.code)
        assertTrue(chunks.all { (it["type"] as JsonPrimitive).content == "chunk" })
    } }

    @Test
    fun recvFileAcksEachChunk() = runTest {
        val acks = mutableListOf<JsonObject>()
        val stream = Stream("u") { acks += it }
        for ((i, part) in listOf("hello ", "there").withIndex()) {
            stream.inbox.send(Json.parseToJsonElement("""{"type": "chunk", "seq": $i, "data": "${Base64.getEncoder().encodeToString(part.toByteArray())}"}""") as JsonObject)
        }
        stream.inbox.send(Json.parseToJsonElement("""{"type": "end"}""") as JsonObject)
        val out = java.io.ByteArrayOutputStream()
        assertEquals(11L, stream.recvFile(out))
        assertEquals("hello there", out.toString())
        assertEquals(listOf(0, 1), acks.map { (it["seq"] as JsonPrimitive).int })
    }
}

class TextEditTest {
    private fun field(text: String, start: Int, end: Int = start, password: Boolean = false) =
        Field("p", 1, password, false, text, start, end)

    @Test
    fun insertAtCursorReplacesSelectionOrAppends() {
        assertEquals(TextEdit.Result("abXc", 3), TextEdit.insert(field("abc", 2), "X"))
        assertEquals(TextEdit.Result("aXd", 2), TextEdit.insert(field("abcd", 1, 3), "X"))
        assertEquals(TextEdit.Result("aXd", 2), TextEdit.insert(field("abcd", 3, 1), "X")) // reversed selection
        assertEquals(TextEdit.Result("abcX", 4), TextEdit.insert(field("abc", -1), "X"))
        assertEquals(TextEdit.Result("abcX", 4), TextEdit.insert(field("abc", 99), "X"))
        assertEquals(TextEdit.Result("secret", 6), TextEdit.insert(field("•••", 3, password = true), "secret"))
    }

    @Test
    fun deletesWholeCharacters() {
        assertEquals(TextEdit.Result("a", 1), TextEdit.deleteBack(field("a😀", 3))) // a surrogate pair at once
        assertEquals(TextEdit.Result("ad", 1), TextEdit.deleteBack(field("abcd", 1, 3)))
        assertEquals(TextEdit.Result("abc", 0), TextEdit.deleteBack(field("abc", 0)))
        assertEquals(TextEdit.Result("bc", 0), TextEdit.deleteForward(field("abc", 0)))
        assertEquals(null, TextEdit.deleteBack(field("•", 1, password = true)))
    }
}

class UiTreeTest {
    @Test
    fun shortNames() {
        assertEquals("url_bar", UiTree.short("com.android.chrome:id/url_bar"))
        assertEquals("TextView", UiTree.short("android.widget.TextView"))
        assertEquals("", UiTree.short(null))
    }

    @Test
    fun emptyAndZeroSizeNodesAreDropped() {
        val roots = listOf(FakeDevice.node("p", "", children = listOf(
            FakeDevice.node("p", "zero", bounds = intArrayOf(5, 5, 5, 9)),
            FakeDevice.node("p", "ok", bounds = intArrayOf(0, 0, 10, 10)),
        )))
        val els = UiTree.elements(roots)
        assertEquals(listOf("ok"), els.map { (it["text"] as JsonPrimitive).content })
        assertEquals(JsonArray(listOf(JsonPrimitive(5), JsonPrimitive(5))), els[0]["center"])
    }
}
