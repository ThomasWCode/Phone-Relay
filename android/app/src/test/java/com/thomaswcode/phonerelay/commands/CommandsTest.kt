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
import kotlinx.serialization.json.JsonNull
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
        assertTrue("chrome_eval" in st["capabilities"]!!.jsonArray.map { it.jsonPrimitive.content }) // through the shell bridge
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
        assertEquals(texts.size, run("ui", """{"engine": "uiautomator"}""")["count"]!!.jsonPrimitive.int)
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
    fun unknownCommandsAreRefused() = runTest {
        assertEquals("invalid_args", error("shell").code)
    }

    @Test
    fun theShellBridgePressesAnyKeyAndTypesWithNoField() = runTest {
        device.shellPaired = true
        run("key", """{"key": "KEYCODE_CAMERA"}""")
        assertEquals("input keyevent KEYCODE_CAMERA", device.shellCommands.last())
        run("key", """{"key": "KEYCODE_CAMERA", "longpress": true}""")
        assertEquals("input keyevent --longpress KEYCODE_CAMERA", device.shellCommands.last())
        device.field = null
        run("key", """{"key": "KEYCODE_A"}""") // no field: a key press, where the app would have typed into one
        assertEquals("input keyevent KEYCODE_A", device.shellCommands.last())
        run("key", """{"key": "back"}""") // what the app can do itself stays its own
        assertEquals(3, device.shellCommands.size)
        val typed = run("type", """{"text": "a b%s\n"}""")
        assertEquals("shell", typed.str("via"))
        assertEquals("input text 'a%sb%' && input text 's' && input keyevent KEYCODE_ENTER", device.shellCommands.last())
        assertEquals("no_text_field", error("type", """{"text": "é"}""").code) // not ASCII: needs a field
        assertEquals("no_text_field", error("type", """{"text": "x", "method": "paste"}""").code)
        device.shellPaired = false
        val e = error("key", """{"key": "KEYCODE_CAMERA"}""")
        assertEquals("unsupported", e.code)
        assertTrue("shell bridge" in e.detail, e.detail)
        assertEquals("no_text_field", error("type", """{"text": "abc"}""").code)
    }

    @Test
    fun theForegroundGuardHoldsThroughTheShellBridgesSetup() = runTest {
        device.shellPaired = true
        device.field = null
        // The owner switches apps while the bridge is being set up: nothing is typed or pressed.
        device.duringShellSetup = { device.fg = "com.example.other" }
        for ((cmd, a) in listOf("type" to """{"text": "abc"""", "key" to """{"key": "KEYCODE_CAMERA"""")) {
            assertEquals("foreground_mismatch", error(cmd, "$a, \"if_foreground\": \"com.android.chrome\"}").code, cmd)
        }
        device.fg = "com.android.chrome"
        assertTrue(device.shellCommands.isEmpty(), device.shellCommands.toString())
        // Long text goes in several commands; the guard is checked before each, not once.
        device.duringShellSetup = null
        var calls = 0
        device.duringShellSetup = { if (++calls == 2) device.fg = "com.example.other" }
        val long = "x".repeat(5000)
        assertEquals("foreground_mismatch", error("type", """{"text": "$long", "if_foreground": "com.android.chrome"}""").code)
        assertEquals(1, device.shellCommands.size) // the first chunk went; the second was refused
    }

    @Test
    fun chromeDevToolsThroughTheShellBridge() = runTest {
        assertEquals("shell_unavailable", error("chrome_tabs").code)
        device.shellPaired = true
        assertEquals(listOf("chrome_devtools_remote", "webview_devtools_remote_4321"), run("chrome_sockets")["sockets"]!!.jsonArray.map { it.jsonPrimitive.content })
        assertEquals(3, run("chrome_tabs")["count"]!!.jsonPrimitive.int)
        val value = run("chrome_eval", """{"expression": "1+1"}""")
        assertEquals(JsonPrimitive(false), value["threw"])
        assertEquals(2, value["value"]!!.jsonPrimitive.int)
        assertEquals("/devtools/page/A", device.chrome.calls.last().first) // the first page by default
        val threw = run("chrome_eval", """{"expression": "throw 1", "tab": 1}""")
        assertEquals("1", threw.str("exception"))
        assertEquals("/devtools/page/B", device.chrome.calls.last().first)
        run("chrome_cdp", """{"method": "Browser.getVersion", "tab": "browser"}""")
        assertEquals("/devtools/browser" to "Browser.getVersion", device.chrome.calls.last().let { it.first to it.second })
        val created = run("chrome_new", """{"url": "https://example.org/new"}""")
        assertEquals("NEW", created["tab"]!!.jsonObject.str("id"))
        assertEquals("Target is closing", run("chrome_close", """{"tab": "NEW"}""").str("reply"))
        assertEquals("Target activated", run("chrome_activate", """{"tab": "A"}""").str("reply"))
        assertEquals("webview_devtools_remote_4321", run("chrome_tabs", """{"socket": "webview_devtools_remote_4321"}""").let { device.devtoolsSockets.last() })
        for ((cmd, bad) in listOf(
            "chrome_cdp" to """{"method": "nodot"}""", "chrome_cdp" to """{"method": "A.b", "params": [1]}""",
            "chrome_tabs" to """{"socket": "a b"}""", "chrome_eval" to """{"expression": "1", "tab": "x y"}""",
            "chrome_activate" to """{"tab": "browser"}""",
        )) assertEquals("invalid_args", error(cmd, bad).code, "$cmd $bad")
        assertEquals("not_found", error("chrome_eval", """{"expression": "1", "tab": 9}""").code)
    }

    @Test
    fun theClipboardIsReadAndBothUiEnginesAreTheTree() = runTest {
        assertEquals("old", run("clipboard_get").str("text"))
        device.clip = null
        assertEquals(JsonNull, run("clipboard_get")["text"])
        assertEquals(run("ui")["count"], run("ui", """{"engine": "uiautomator"}""")["count"])
        assertEquals("invalid_args", error("ui", """{"engine": "xml"}""").code)
    }

    private fun sink(sent: MutableList<JsonObject>) = Stream("r") { sent += it }

    @Test
    fun recordingStartsStopsAndStreamsItsVideo() = runTest {
        val started = run("record_start", """{"seconds": 5, "size": "720x1560", "bit_rate": 4000000}""")
        assertEquals(JsonPrimitive(true), started["recording"])
        assertEquals(Triple(5.0, Size(720, 1560), 4_000_000), device.recordings.last())
        val sent = mutableListOf<JsonObject>()
        val stopped = commands.handle("record_stop", args("{}"), sink(sent))
        assertEquals(1, stopped["segments"]!!.jsonPrimitive.int)
        assertEquals("video/mp4", sent.first().str("mime"))
        assertEquals("1", sent.first()["headers"]!!.jsonObject.str("X-Segments"))
        assertEquals("mp4 bytes", String(Base64.getDecoder().decode(sent.first { it.str("type") == "chunk" }.str("data"))))
        assertTrue(FakeDevice.RECORDING !in device.files) // sent, then deleted
        assertEquals("not_found", assertFailsWith<CommandError> { commands.handle("record_stop", args("{}"), sink(sent)) }.code)
        // /record: start, the wait, stop; the default bit rate as the adb client's.
        val whole = commands.handle("record", args("""{"seconds": 2}"""), sink(mutableListOf()))
        assertEquals(1, whole["segments"]!!.jsonPrimitive.int)
        assertEquals(Triple(2.0, null, Commands.RECORD_BIT_RATE), device.recordings.last())
        for (bad in listOf("""{"seconds": 0}""", """{"size": "720"}""", """{"size": 720}""", """{"bit_rate": "x"}""", """{"bit_rate": 0}""")) {
            assertEquals("invalid_args", error("record_start", bad).code, bad)
        }
    }

    @Test
    fun audioIsStreamedAsWav() = runTest {
        val sent = mutableListOf<JsonObject>()
        val r = commands.handle("audio", args("""{"seconds": 2, "source": "mic"}"""), sink(sent))
        assertEquals("mic", r.str("source"))
        assertEquals(48, r["bytes"]!!.jsonPrimitive.int)
        assertEquals("audio/wav", sent.first().str("mime"))
        assertEquals("mic", sent.first()["headers"]!!.jsonObject.str("X-Audio-Source"))
        assertEquals(2.0 to "mic", device.audioTaken.last())
        commands.handle("audio", args("{}"), sink(mutableListOf()))
        assertEquals(10.0 to "output", device.audioTaken.last())
        assertTrue(FakeDevice.AUDIO !in device.files)
        assertEquals("invalid_args", assertFailsWith<CommandError> { commands.handle("audio", args("""{"source": "speaker"}"""), sink(sent)) }.code)
    }

    @Test
    fun aClosedConnectionEndsTheCapture() = runTest {
        run("record_start")
        commands.closed()
        assertEquals(1, device.captureEnded)
        assertEquals("not_found", assertFailsWith<CommandError> { commands.handle("record_stop", args("{}"), sink(mutableListOf())) }.code)
    }

    @Test
    fun timeouts() = runTest {
        device.gestureDelayMs = 5_000
        assertEquals("timeout", error("tap", """{"x": 1, "y": 1, "timeout": 1}""").code)
        device.gestureDelayMs = 500
        run("tap", """{"x": 1, "y": 1, "timeout": 86400}""")
        assertEquals("invalid_args", error("tap", """{"x": 1, "y": 1, "timeout": 0}""").code)
        assertEquals(130.0, defaultTimeout("type", args("""{"text": "${"x".repeat(1000)}"}""")))
        assertEquals(60.0 + 40, defaultTimeout("record", args("""{"seconds": 20}""")))
        assertEquals(60.0 + 20, defaultTimeout("audio", args("{}")))
        assertEquals(TRANSFER_TIMEOUT_S, defaultTimeout("record_stop", args("{}")))
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
    fun requestAcceptedRunsTheQueuedStepsAtOnce() = runTest {
        device.answer = Answer(true, "not WhatsApp")
        val res = run("request", """{"reason": "Check the bus times", "seconds": 60, "steps": [
            {"cmd": "launch", "args": {"package": "com.android.chrome"}}, {"cmd": "type", "args": {"text": "bus 7"}}]}""")
        assertEquals("accepted", res.str("answer"))
        assertEquals("not WhatsApp", res.str("message"))
        assertEquals(listOf("com.android.chrome"), device.launched)
        assertEquals(JsonPrimitive(true), res["batch"]!!.jsonObject["all_ok"])
        // The owner saw the reason, each step in words (typed text included) and the wait.
        assertEquals(Triple("Check the bus times", listOf("Open com.android.chrome", "Type “bus 7”"), 60.0), device.asked.single())
    }

    @Test
    fun requestDeniedOrExpiredRunsNothing() = runTest {
        device.answer = Answer(false, "after 6pm")
        val denied = run("request", """{"reason": "x", "steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}]}""")
        assertEquals("denied", denied.str("answer"))
        assertEquals("after 6pm", denied.str("message"))
        assertEquals(null, denied["batch"])
        device.answer = Answer(null, null)
        val expired = run("request", """{"reason": "x", "steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}]}""")
        assertEquals("expired", expired.str("answer"))
        assertEquals(JsonNull, expired["message"])
        assertTrue(device.gestures.isEmpty())
        assertEquals(DEFAULT_REQUEST_S, device.asked.last().third)
        // Accepted with nothing queued: just the answer.
        device.answer = Answer(true, null)
        assertEquals(null, run("request", """{"reason": "x"}""")["batch"])
    }

    @Test
    fun requestGuardsTheStepsNotTheAsking() = runTest {
        device.fg = "com.android.settings" // the owner is elsewhere when the agent asks
        val res = run("request", """{"reason": "x", "if_foreground": "com.android.chrome", "steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}]}""")
        assertEquals("accepted", res.str("answer"))
        assertEquals("foreground_mismatch", res["batch"]!!.jsonObject["results"]!!.jsonArray[0].jsonObject.str("error"))
        for (bad in listOf("""{}""", """{"reason": " "}""", """{"reason": "x", "seconds": 0}""", """{"reason": "x", "steps": []}""",
            """{"reason": "x", "steps": [{"cmd": "request"}]}""")) {
            assertEquals("invalid_args", error("request", bad).code, bad)
        }
        assertEquals(1, device.asked.size) // a bad request is refused before the owner sees it
        assertEquals(300.0 + 30 + 30, defaultTimeout("request", args("""{"steps": [{"cmd": "tap", "args": {}}]}""")))
        assertEquals(60.0 + 30, defaultTimeout("request", args("""{"seconds": 60}""")))
    }

    @Test
    fun askDescribesSteps() {
        fun d(json: String) = Ask.describe(args(json))
        assertEquals("Tap at 540, 1170 (only in com.android.chrome)", d("""{"cmd": "tap", "args": {"x": 540, "y": 1170, "if_foreground": "com.android.chrome"}}"""))
        assertEquals("Pause 500 ms", d("""{"cmd": "sleep", "args": {"ms": 500}}"""))
        assertEquals("Press back (held)", d("""{"cmd": "key", "args": {"key": "back", "longpress": true}}"""))
        assertEquals("Open https://example.com in com.android.chrome", d("""{"cmd": "open", "args": {"url": "https://example.com", "package": "com.android.chrome"}}"""))
        assertEquals("Wait for the screen to settle", d("""{"cmd": "wait", "args": {"for": "stable"}}"""))
        assertEquals("Empty the clipboard", d("""{"cmd": "clipboard_set", "args": {"text": null}}"""))
        val long = d("""{"cmd": "type", "args": {"text": "${"a".repeat(200)}"}}""")
        assertEquals(Ask.TEXT_MAX + 7, long.length) // Type + the quotes around TEXT_MAX characters, the last an ellipsis
        assertTrue(long.endsWith("…”"))
    }

    @Test
    fun batchRunsChromeSteps() = runTest {
        // The relay and the adb client let a batch run DevTools commands; the app did not.
        device.shellPaired = true
        val res = run("batch", """{"steps": [{"cmd": "chrome_tabs"}, {"cmd": "chrome_eval", "args": {"expression": "1+1"}}]}""")
        assertEquals(JsonPrimitive(true), res["all_ok"])
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

    @Test
    fun aFailedUploadLeavesTheOldFileAsItWas() = runTest {
        device.written["/sdcard/Download/keep.txt"] = "the old file".toByteArray()
        val stream = Stream("u") { }
        stream.inbox.trySend(args("""{"type": "chunk", "seq": 0, "data": "${Base64.getEncoder().encodeToString("half".toByteArray())}"}"""))
        stream.inbox.trySend(args("""{"type": "cancel"}"""))
        assertEquals("cancelled", assertFailsWith<CommandError> {
            commands.handle("push", args("""{"path": "/sdcard/Download/keep.txt"}"""), stream)
        }.code)
        assertEquals("the old file", String(device.written["/sdcard/Download/keep.txt"]!!))
        assertEquals(setOf("/sdcard/Download/keep.txt"), device.written.keys) // the .part file is gone
    }

    @Test
    fun aZeroIntervalWaitStillStopsAtItsDeadline() = runBlocking {
        device.fg = "com.android.chrome"
        val started = System.nanoTime()
        val e = assertFailsWith<CommandError> {
            run("wait", """{"for": "foreground", "package": "com.example.app", "interval_ms": 0, "seconds": 3600, "timeout": 0.3}""")
        }
        assertEquals("timeout", e.code)
        assertTrue((System.nanoTime() - started) / 1_000_000 < 3000)
        assertEquals("timeout", assertFailsWith<CommandError> {
            run("wait", """{"for": "text", "text": "never", "interval_ms": 0, "seconds": 3600, "timeout": 0.3}""")
        }.code)
    }

    // ---- releases ----

    @Test
    fun installOverTheInstalledApp() = runTest {
        device.shellPaired = true
        device.shellReply = { if (it.startsWith("cat /sdcard/Download/tiny.apk | pm install")) "Success\n" else null }
        device.packages["com.example.tiny"] = device.packages["com.example.tiny"]!!.copy(versionName = "1.2.3", versionCode = 42)
        val r = run("install", """{"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny", "if_foreground": "com.android.chrome"}""")
        assertEquals("1.2.3", r.str("version_name"))
        assertEquals(42, r["version_code"]!!.jsonPrimitive.int)
        // Streamed in by the shell: the system server cannot open a file in shared storage itself
        assertEquals("cat /sdcard/Download/tiny.apk | pm install -r -S $(stat -c %s /sdcard/Download/tiny.apk) 2>&1 || true",
            device.shellCommands.single())
    }

    @Test
    fun installRefusesAnotherPackageAndReportsAndroidsError() = runTest {
        device.shellPaired = true
        assertEquals("package_mismatch", error("install", """{"path": "/sdcard/Download/tiny.apk", "package": "com.android.chrome"}""").code)
        assertTrue(device.shellCommands.isEmpty())
        device.shellReply = { "Failure [INSTALL_FAILED_VERSION_DOWNGRADE: Downgrade detected]\n" }
        val e = error("install", """{"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny"}""")
        assertEquals("install_failed", e.code)
        assertEquals("INSTALL_FAILED_VERSION_DOWNGRADE: Downgrade detected", e.detail)
        // An error that is no Failure [...] line comes back from its start, not the stack's last frames
        device.shellReply = { "Error: Can't open file: x\n\nException occurred while executing 'install':\n" + "\tat a.b.C.d(C.java:1)\n".repeat(40) }
        assertTrue(error("install", """{"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny"}""").detail.startsWith("Error: Can't open file"))
        assertEquals("not_found", error("install", """{"path": "/sdcard/Download/none.apk", "package": "com.example.tiny"}""").code)
        // The app in front is checked again just before the install, once the bridge is up.
        device.shellCommands.clear()
        device.duringShellSetup = { device.fg = "com.android.settings" }
        assertEquals("foreground_mismatch",
            error("install", """{"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny", "if_foreground": "com.android.chrome"}""").code)
        assertTrue(device.shellCommands.isEmpty())
    }

    @Test
    fun accessibilityLogcatAndSettingsGoThroughTheShell() = runTest {
        assertEquals("shell_unavailable", error("accessibility").code)
        device.shellPaired = true
        device.shellReply = { cmd ->
            when {
                cmd == ShellCommands.ACCESSIBILITY -> "com.a/.S:com.b/com.b.T\n${ShellCommands.SPLIT_MARKER}\n" +
                    "     Bound services:{Service[label=A, feedbackType[FEEDBACK_GENERIC], id=com.a/.S, eventTypes=TYPES_ALL_MASK]}\n" +
                    "     Enabled services:{{com.a/com.a.S}, {com.b/com.b.T}}\n"
                cmd.startsWith("logcat") -> "one\ntwo\nthree\n"
                cmd.startsWith("settings get global") -> "1\n${ShellCommands.SPLIT_MARKER}\nNight mode: yes\n"
                else -> null
            }
        }
        val a = run("accessibility")
        assertEquals(listOf("com.a/com.a.S", "com.b/com.b.T"), a["enabled"]!!.jsonArray.map { it.jsonPrimitive.content })
        assertEquals(listOf("com.b/com.b.T"), a["missing"]!!.jsonArray.map { it.jsonPrimitive.content })
        val log = run("logcat", """{"tag": ["Decrastination"], "level": "W", "since": 60, "lines": 2}""")
        assertEquals(listOf("two", "three"), log["lines"]!!.jsonArray.map { it.jsonPrimitive.content })
        assertEquals("true", log.str("truncated"))
        assertEquals("logcat -d -t \"$(( $(date +%s) - 60 )).000\" -s Decrastination:W 2>&1 | tail -n 3", device.shellCommands.last())
        val st = run("settings", """{"namespace": "global", "key": "stay_on_while_plugged_in"}""")
        assertEquals("1", st.str("value"))
        assertEquals("yes", st.str("night_mode"))
        assertEquals("invalid_args", error("settings", """{"namespace": "global", "key": "a;reboot"}""").code)
    }

    @Test
    fun packageAndItsApkFromPackageManager() = runTest {
        val p = run("package", """{"name": "com.example.tiny"}""") // no shell needed
        assertEquals(41, p["version_code"]!!.jsonPrimitive.int)
        assertEquals("2026-11-09 06:58:42", p.str("last_update_time"))
        assertEquals(listOf("ab12"), p["signing_sha256"]!!.jsonArray.map { it.jsonPrimitive.content })
        assertEquals("not_found", error("package", """{"name": "com.example.missing"}""").code)
        val sent = mutableListOf<JsonObject>()
        val r = commands.handle("package_apk", args("""{"name": "com.example.tiny"}"""), sink(sent))
        assertEquals(Commands.APK_MIME, sent.first().str("mime"))
        assertEquals("41", sent.first()["headers"]!!.jsonObject.str("X-Version-Code"))
        assertEquals("fake apk bytes", String(Base64.getDecoder().decode(sent.first { it.str("type") == "chunk" }.str("data"))))
        assertEquals(FakeDevice.TINY_INSTALLED, r.str("path"))
    }

    @Test
    fun runAsReadsADebuggableAppsFilesOnly() = runTest {
        device.shellPaired = true
        device.shellReply = { cmd ->
            when {
                cmd.startsWith("run-as com.example.tiny stat") -> "regular file|12|1700000000|files/plan.json\n"
                cmd.startsWith("run-as com.example.tiny sh -c") -> "regular file|12|1700000000|plan.json\ndirectory|4096|1700000000|crashes\n"
                else -> null
            }
        }
        device.shellBytes = { """{"plan": []}""".toByteArray() }
        val sent = mutableListOf<JsonObject>()
        val r = commands.handle("run_as", args("""{"package": "com.example.tiny", "path": "files/plan.json"}"""), sink(sent))
        assertEquals(12, r["bytes"]!!.jsonPrimitive.int)
        assertEquals("""{"plan": []}""", String(Base64.getDecoder().decode(sent.first { it.str("type") == "chunk" }.str("data"))))
        assertEquals("run-as com.example.tiny cat files/plan.json", device.shellCommands.last())
        assertTrue(FakeDevice.SHELL_OUT !in device.files) // sent, then deleted
        val listing = run("run_as_files", """{"package": "com.example.tiny", "path": "files"}""")
        assertEquals(listOf("plan.json", "crashes"), listing["entries"]!!.jsonArray.map { it.jsonObject.str("name") })
        assertEquals("not_debuggable", assertFailsWith<CommandError> {
            commands.handle("run_as", args("""{"package": "com.android.chrome", "path": "files/x"}"""), sink(sent))
        }.code)
        assertEquals("invalid_args", assertFailsWith<CommandError> {
            commands.handle("run_as", args("""{"package": "com.example.tiny", "path": "../x"}"""), sink(sent))
        }.code)
        // A short read (run-as's error in place of the file) is not passed on as the file
        device.shellBytes = { "run-as: not allowed".toByteArray() }
        assertEquals("device_error", assertFailsWith<CommandError> {
            commands.handle("run_as", args("""{"package": "com.example.tiny", "path": "files/plan.json"}"""), sink(mutableListOf()))
        }.code)
    }

    @Test
    fun intentAsTheShellNeedsAComponent() = runTest {
        device.shellPaired = true
        device.shellReply = { "Starting: Intent { cmp=com.example.tiny/.debug.Command }\nStatus: ok\n" }
        run("intent", """{"component": "com.example.tiny/.debug.Command", "extras": {"cmd": "backup-now", "n": 3}, "flags": 268435456, "as_shell": true}""")
        assertEquals("am start -W -n com.example.tiny/.debug.Command -f 268435456 --es cmd backup-now --ei n 3 || true", device.shellCommands.single())
        assertTrue(device.intents.isEmpty()) // not sent by the app itself
        assertEquals("invalid_args", error("intent", """{"action": "a", "as_shell": true}""").code)
        device.shellReply = { "Error: Activity not started, unable to resolve Intent\n" }
        assertEquals("launch_failed", error("intent", """{"component": "com.a/.B", "as_shell": true}""").code)
        assertTrue("install" in commands.capabilities && "run_as" in commands.capabilities)
    }


    // ---- what Decrastination's workflow does over adb ----

    @Test
    fun contentQuery() = runTest {
        device.shellPaired = true
        device.shellReply = { if (it.startsWith("content query")) "Row: 0 _id=1, description=Made up, with a comma\nand a line\nRow: 1 _id=2, description=NULL\n" else null }
        val r = run("content", """{"uri": "content://com.android.calendar/events", "projection": ["_id", "description"], "where": "calendar_id=3 AND deleted=0"}""")
        assertEquals(2, r["count"]!!.jsonPrimitive.int)
        assertEquals("Made up, with a comma\nand a line", r["rows"]!!.jsonArray[0].jsonObject.str("description"))
        assertEquals(JsonNull, r["rows"]!!.jsonArray[1].jsonObject["description"])
        assertEquals("content query --uri content://com.android.calendar/events --projection _id:description --where 'calendar_id=3 AND deleted=0' 2>&1",
            device.shellCommands.last())
        device.shellReply = { "Error while accessing provider:x\njava.lang.IllegalArgumentException: Unknown URL\n" + "\tat a.b.C.d(C.java:1)\n".repeat(40) }
        val failed = error("content", """{"uri": "content://x/y"}""")
        assertEquals("content_failed", failed.code)
        // The error's first lines, not the stack's last frames
        assertTrue(failed.detail.startsWith("Error while accessing provider:x\njava.lang.IllegalArgumentException"))
        // A value's later line that starts like an error is still the value
        assertEquals(listOf(mapOf("description" to "Notes\nException handling workshop")),
            ShellCommands.parseContent("Row: 0 description=Notes\nException handling workshop\n", listOf("description")))
    }

    @Test
    fun runAsArchivePushDeleteAndKill() = runTest {
        device.shellPaired = true
        val tar = ByteArray(1024).also { "ustar".toByteArray().copyInto(it, 257) }
        device.shellBytes = { tar }
        val sent = mutableListOf<JsonObject>()
        commands.handle("run_as_archive", args("""{"package": "com.example.tiny", "path": "files"}"""), sink(sent))
        assertEquals("application/x-tar", sent.first().str("mime"))
        assertEquals("run-as com.example.tiny sh -c 'tar -cf - files 2>/dev/null || echo PHONE_RELAY_TAR_FAILED'", device.shellCommands.last())
        device.shellBytes = { "PHONE_RELAY_TAR_FAILED\n".toByteArray() }
        assertEquals("not_found", assertFailsWith<CommandError> {
            commands.handle("run_as_archive", args("""{"package": "com.example.tiny", "path": "nothing"}"""), sink(mutableListOf()))
        }.code)
        // tar left out a file it could not read: its header is whole, but the backup is not, so nothing is sent
        device.shellBytes = { tar + "PHONE_RELAY_TAR_FAILED\n".toByteArray() }
        val none = mutableListOf<JsonObject>()
        assertEquals("device_error", assertFailsWith<CommandError> {
            commands.handle("run_as_archive", args("""{"package": "com.example.tiny", "path": "files"}"""), sink(none))
        }.code)
        assertTrue(none.isEmpty())

        val data = """{"made": "up"}""".toByteArray()
        device.shellReply = { cmd -> if (cmd.startsWith("run-as com.example.tiny stat -c %s")) "${data.size}\n" else null }
        fun upload() = Stream("u") { }.also {
            it.inbox.trySend(kotlinx.serialization.json.buildJsonObject {
                put("type", JsonPrimitive("chunk")); put("seq", JsonPrimitive(0)); put("data", JsonPrimitive(Base64.getEncoder().encodeToString(data)))
            })
            it.inbox.trySend(kotlinx.serialization.json.buildJsonObject { put("type", JsonPrimitive("end")) })
        }
        val upload = Stream("u") { }
        upload.inbox.trySend(kotlinx.serialization.json.buildJsonObject {
            put("type", JsonPrimitive("chunk")); put("seq", JsonPrimitive(0)); put("data", JsonPrimitive(Base64.getEncoder().encodeToString(data)))
        })
        upload.inbox.trySend(kotlinx.serialization.json.buildJsonObject { put("type", JsonPrimitive("end")) })
        val pushed = commands.handle("run_as_push", args("""{"package": "com.example.tiny", "path": "files/credentials.import"}"""), upload)
        assertEquals(data.size, pushed["bytes"]!!.jsonPrimitive.int)
        assertEquals("run-as com.example.tiny sh -c 'mkdir -p files && cat > files/.credentials.import.part'", device.shellInputs.single().first)
        assertTrue(device.shellInputs.single().second.contentEquals(data))
        assertEquals("run-as com.example.tiny mv -f -- files/.credentials.import.part files/credentials.import 2>&1", device.shellCommands.last())
        assertTrue(FakeDevice.UPLOAD !in device.written) // the app's own copy is gone
        // An existing folder is refused before anything is sent, so mv cannot put the file inside it
        device.shellReply = { cmd -> if (cmd.startsWith("run-as com.example.tiny stat -c '")) "directory|4096|1700000000|files\n" else null }
        assertEquals("invalid_args", assertFailsWith<CommandError> {
            commands.handle("run_as_push", args("""{"package": "com.example.tiny", "path": "files"}"""), upload())
        }.code)
        assertEquals(1, device.shellInputs.size)

        device.shellReply = { cmd -> if (cmd.startsWith("run-as com.example.tiny stat -c '")) "regular file|14|1700000000|files/ai-prompts.json\n" else null }
        run("run_as_delete", """{"package": "com.example.tiny", "path": "files/ai-prompts.json"}""")
        assertEquals("run-as com.example.tiny rm -f -- files/ai-prompts.json 2>&1", device.shellCommands.last())
        for (bad in listOf("""{"package": "com.example.tiny", "path": "files/"}""", """{"package": "com.example.tiny", "path": "-rf"}""")) {
            assertEquals("invalid_args", error("run_as_delete", bad).code, bad)
        }
        assertEquals("not_debuggable", error("run_as_delete", """{"package": "com.android.chrome", "path": "files/x"}""").code)

        device.shellReply = { cmd -> if (cmd.startsWith("pidof")) "4321\n" else null }
        val killed = run("run_as_kill", """{"package": "com.example.tiny", "signal": "STOP"}""")
        assertEquals(4321, killed["pid"]!!.jsonPrimitive.int)
        assertEquals("run-as com.example.tiny kill -STOP 4321 2>&1", device.shellCommands.last())
        device.shellReply = { null }
        assertEquals("not_found", error("run_as_kill", """{"package": "com.example.tiny", "signal": "KILL"}""").code)
        assertEquals("invalid_args", error("run_as_kill", """{"package": "com.example.tiny", "signal": "TERM"}""").code)
    }

    @Test
    fun runAsPushRechecksTheForegroundBeforeReplacing() = runTest {
        device.shellPaired = true
        val data = "new".toByteArray()
        device.shellReply = { cmd ->
            if (cmd.startsWith("run-as com.example.tiny stat -c %s")) {
                device.fg = "com.android.settings" // the owner switched apps during the upload
                "${data.size}\n"
            } else null
        }
        val upload = Stream("u") { }
        upload.inbox.trySend(kotlinx.serialization.json.buildJsonObject {
            put("type", JsonPrimitive("chunk")); put("seq", JsonPrimitive(0)); put("data", JsonPrimitive(Base64.getEncoder().encodeToString(data)))
        })
        upload.inbox.trySend(kotlinx.serialization.json.buildJsonObject { put("type", JsonPrimitive("end")) })
        assertEquals("foreground_mismatch", assertFailsWith<CommandError> {
            commands.handle("run_as_push", args("""{"package": "com.example.tiny", "path": "files/plan.json", "if_foreground": "com.android.chrome"}"""), upload)
        }.code)
        assertTrue(device.shellCommands.none { it.contains(" mv ") })
        assertEquals("run-as com.example.tiny rm -f -- files/.plan.json.part 2>&1", device.shellCommands.last())
    }

    @Test
    fun runAsDeleteAndKillRecheckTheForeground() = runTest {
        device.shellPaired = true
        device.shellReply = { cmd ->
            when {
                cmd.startsWith("run-as com.example.tiny stat -c '") -> {
                    device.fg = "com.android.settings" // the owner switched apps after the lookup
                    "regular file|14|1700000000|files/plan.json\n"
                }
                cmd.startsWith("pidof") -> { device.fg = "com.android.settings"; "4321\n" }
                else -> null
            }
        }
        for ((cmd, json) in listOf(
            "run_as_delete" to """{"package": "com.example.tiny", "path": "files/plan.json", "if_foreground": "com.android.chrome"}""",
            "run_as_kill" to """{"package": "com.example.tiny", "signal": "STOP", "if_foreground": "com.android.chrome"}""",
        )) {
            device.fg = "com.android.chrome"
            assertEquals("foreground_mismatch", error(cmd, json).code, cmd)
        }
        assertTrue(device.shellCommands.none { it.contains(" rm ") || it.contains(" kill ") })
    }

    @Test
    fun nightModeAndForceStop() = runTest {
        device.shellPaired = true
        var night = "yes"
        device.shellReply = { cmd ->
            when {
                cmd.startsWith("cmd uimode night ") -> { night = cmd.substringAfterLast(' '); "Night mode: $night\n" }
                cmd == "cmd uimode night" -> "Night mode: $night\n"
                cmd == "settings get secure enabled_accessibility_services" -> "com.example.tiny/.FocusService\n"
                else -> null
            }
        }
        val r = run("night_mode", """{"mode": "no"}""")
        assertEquals("no", r.str("night_mode"))
        assertEquals("yes", r.str("previous"))
        run("force_stop", """{"package": "com.android.chrome"}""")
        assertEquals("am force-stop com.android.chrome", device.shellCommands.last())
        for (pkg in listOf("com.example.tiny", "com.android.systemui", "com.thomaswcode.phonerelay")) {
            assertEquals("stop_refused", error("force_stop", """{"package": "$pkg"}""").code, pkg)
        }
        assertEquals("not_found", error("force_stop", """{"package": "com.example.missing"}""").code)
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

class PathsTest {
    private val corner = listOf(Point(0f, 0f), Point(100f, 0f), Point(100f, 100f))

    @Test
    fun aLongGestureSplitsIntoSlicesThatMeet() {
        assertEquals(1, Paths.slices(1, 60_000))
        assertEquals(1, Paths.slices(60_000, 60_000))
        assertEquals(2, Paths.slices(60_001, 60_000))
        assertEquals(3, Paths.slices(150_000, 60_000))
        assertEquals(listOf(Point(0f, 0f), Point(100f, 0f)), Paths.slice(corner, 0.0, 0.5))
        assertEquals(listOf(Point(100f, 0f), Point(100f, 100f)), Paths.slice(corner, 0.5, 1.0))
        assertEquals(corner, Paths.slice(corner, 0.0, 1.0))
        val n = 3
        val parts = (0 until n).map { Paths.slice(corner, it.toDouble() / n, (it + 1).toDouble() / n) }
        for (i in 0 until n - 1) assertEquals(parts[i].last(), parts[i + 1].first())
        assertEquals(listOf(Point(5f, 5f)), Paths.slice(listOf(Point(5f, 5f)), 0.25, 0.5)) // a held finger
    }
}

class MediaTest {
    @Test
    fun wavHeader() {
        val h = Wav.header(192_000)
        assertEquals(44, h.size)
        assertEquals("RIFF", String(h, 0, 4))
        assertEquals("WAVEfmt ", String(h, 8, 8))
        assertEquals("data", String(h, 36, 4))
        fun le(at: Int, n: Int) = (0 until n).sumOf { (h[at + it].toLong() and 0xFF) shl (8 * it) }
        assertEquals(36L + 192_000, le(4, 4))
        assertEquals(48_000L, le(24, 4))
        assertEquals(192_000L, le(40, 4))
        assertEquals(192_000L, Wav.dataBytes(1.0))
    }

    @Test
    fun videoSizeFitsTheEncoder() {
        assertEquals(Size(1072, 2336), Video.fit(Size(1080, 2340), 16, 16) { _, _ -> true })
        assertEquals(Size(810, 1754), Video.fit(Size(1080, 2340), 2, 2) { _, h -> h <= 1920 })
        assertEquals(null, Video.fit(Size(1080, 2340), 2, 2) { _, _ -> false })
    }
}

class ConsentTest {
    private fun prompt(pkg: String = Consent.SYSTEM_UI, title: String = "Start recording or casting with Phone Relay?", start: UiNode) =
        FakeDevice.node(pkg, "", children = listOf(
            FakeDevice.node(pkg, title),
            FakeDevice.node(pkg, "Cancel", id = "$pkg:id/button_cancel", clickable = true),
            start,
        ))

    @Test
    fun onlyTheAppsPromptsStartButtonIsPressed() {
        val start = FakeDevice.node(Consent.SYSTEM_UI, "Start", clickable = true)
        assertEquals(start, Consent.startButton(prompt(start = start), "Phone Relay"))
        assertEquals(null, Consent.startButton(prompt(title = "Start recording or casting with Another App?", start = start), "Phone Relay"))
        val elsewhere = FakeDevice.node("com.example.app", "Start", clickable = true)
        assertEquals(null, Consent.startButton(prompt(pkg = "com.example.app", start = elsewhere), "Phone Relay"))
        // A button whose words are on a child; one known only by its id.
        val wrapped = FakeDevice.node(Consent.SYSTEM_UI, "", clickable = true, children = listOf(FakeDevice.node(Consent.SYSTEM_UI, "Start now")))
        assertEquals(wrapped, Consent.startButton(prompt(start = wrapped), "Phone Relay"))
        val byId = FakeDevice.node(Consent.SYSTEM_UI, "OK", id = "com.android.systemui:id/button_start", clickable = true)
        assertEquals(byId, Consent.startButton(prompt(start = byId), "Phone Relay"))
        val notClickable = FakeDevice.node(Consent.SYSTEM_UI, "Start")
        assertEquals(null, Consent.startButton(prompt(start = notClickable), "Phone Relay"))
    }
}

class ShellTextTest {
    @Test
    fun typesAsTheAdbClientDoes() {
        assertEquals("'it'\\''s%sok'", ShellText.escape("it's ok"))
        assertEquals("input text '100%'", ShellText.inputText("100%"))
        assertEquals("input text 'a%' && input text 's%sb'", ShellText.inputText("a%s b"))
        assertEquals(listOf("input text 'a' && input keyevent KEYCODE_TAB && input text 'b'"), ShellText.commands("a\tb"))
        assertEquals(listOf("input keyevent KEYCODE_ENTER && input keyevent KEYCODE_ENTER"), ShellText.commands("\r\n\r"))
        assertEquals(2, ShellText.commands("x".repeat(5000)).size) // chunks of 1000, joined while short
        assertTrue(ShellText.canType("a\nb"))
        assertTrue(!ShellText.canType("é") && !ShellText.canType("") && !ShellText.canType("a\u0007"))
    }
}

class ShellCommandsTest {
    private fun args(json: String) = Json.parseToJsonElement(json) as JsonObject

    @Test
    fun quotesAsPythonsShlex() {
        assertEquals("files/plan.json", ShellCommands.quote("files/plan.json"))
        assertEquals("'a b'", ShellCommands.quote("a b"))
        assertEquals("'it'\"'\"'s'", ShellCommands.quote("it's"))
        assertEquals("''", ShellCommands.quote(""))
    }

    @Test
    fun logcatCommandsAsTheAdbClientsAre() {
        assertEquals("logcat -d 2>&1 | tail -n 501" to 500, ShellCommands.logcatCommand(args("{}")))
        assertEquals(
            "logcat -d -b crash -t \"$(( $(date +%s) - 60 )).000\" -s Decrastination:W AndroidRuntime:W 2>&1 | tail -n 11",
            ShellCommands.logcatCommand(args("""{"tag": ["Decrastination", "AndroidRuntime"], "level": "W", "since": 60, "lines": 10, "buffer": "crash"}""")).first,
        )
        assertEquals("logcat -d -s 'My\$Tag:V' 2>&1 | tail -n 501", ShellCommands.logcatCommand(args("""{"tag": ["My${'$'}Tag"]}""")).first)
        assertEquals("logcat -d -t '2026-11-09 06:58:42.000' '*:E' 2>&1 | tail -n 501",
            ShellCommands.logcatCommand(args("""{"since": "2026-11-09 06:58:42", "level": "E"}""")).first)
        for (bad in listOf("""{"tag": "a;b"}""", """{"level": "X"}""", """{"since": "now"}""", """{"since": 0}""", """{"lines": 0}""", """{"lines": 1.5}""", """{"buffer": "x"}""", """{"tag": []}""")) {
            assertFailsWith<CommandError>(bad) { ShellCommands.logcatCommand(args(bad)) }
        }
        val r = ShellCommands.logcatResult("a\nb\nc\n", 2)
        assertEquals(listOf("b", "c"), r.lines)
        assertTrue(r.truncated)
        assertEquals(emptyList(), ShellCommands.logcatResult("", 2).lines)
    }

    @Test
    fun accessibilityInstallAndSettingsOutput() {
        val dump = "     Bound services:{Service[label=A, id=com.a/.S, eventTypes=1], \n                     Service[label=B, id=com.b/com.b.T]}\n     Enabled services:{{com.a/com.a.S}}\n"
        val a = ShellCommands.parseAccessibility("com.a/.S:com.b/.T:com.c/.U\n${ShellCommands.SPLIT_MARKER}\n$dump")
        assertEquals(listOf("com.a/com.a.S", "com.b/com.b.T"), a.bound)
        assertEquals(listOf("com.c/com.c.U"), a.missing)
        val unnamed = ShellCommands.parseAccessibility("com.a/.S\n${ShellCommands.SPLIT_MARKER}\n     Bound services:{Service[label=A]}\n     Enabled services:{}\n")
        assertEquals(null, unnamed.bound)
        assertEquals(emptyList(), ShellCommands.parseAccessibility("null\n${ShellCommands.SPLIT_MARKER}\n").enabled)
        assertEquals(null, ShellCommands.parseInstall("Performing Streamed Install\nSuccess\n"))
        assertEquals("INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match",
            ShellCommands.parseInstall("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]\n"))
        assertEquals("INSTALL_FAILED_INVALID_APK", ShellCommands.parseInstall("Failure [INSTALL_FAILED_INVALID_APK]\n"))
        assertEquals(null to "auto", ShellCommands.parseSettings("null\n${ShellCommands.SPLIT_MARKER}\nNight mode: auto\n"))
    }

    @Test
    fun anAppsOwnFiles() {
        assertEquals("files/plan.json", ShellCommands.dataPath(args("""{"path": "files/plan.json"}""")))
        for (bad in listOf("/data/x", "../x", "files/../../x", "a//b", "a;b", "")) {
            assertFailsWith<CommandError>(bad) { ShellCommands.dataPath(args("""{"path": "$bad"}""")) }
        }
        assertEquals(null, ShellCommands.dataPath(args("{}"), required = false))
        assertEquals(FileEntry("plan.json", "file", 12, 1700000000), ShellCommands.parseStat("regular file|12|1700000000|plan.json"))
        assertEquals(null, ShellCommands.parseStat(ShellCommands.MISSING_MARKER))
        assertEquals("run-as com.a stat -c '%F|%s|%Y|%n' -- 'a b.json' 2>/dev/null || echo PHONE_RELAY_MISSING", ShellCommands.runAsStat("com.a", "a b.json"))
    }

    @Test
    fun anIntentAsTheShellUser() {
        val spec = IntentSpec(action = "a.B", data = "x y", component = "com.a/.C", pkg = "com.a", categories = listOf("c"), flags = 1,
            extras = args("""{"s": "it's", "i": 1, "l": 4294967296, "f": 1.5, "b": true, "z": null}"""), broadcast = true)
        assertEquals("am broadcast -a a.B -d 'x y' -n com.a/.C -p com.a -c c -f 1 --es s 'it'\"'\"'s' --ei i 1 --el l 4294967296 --ef f 1.5 --ez b true --esn z || true",
            ShellCommands.am(spec))
    }
}
