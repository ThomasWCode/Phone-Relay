package com.thomaswcode.phonerelay.commands

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put

/** One DevTools socket, reached through the app's shell bridge (AndroidDevice) or a fake (tests). */
interface DevToolsLink {
    /** One request to DevTools' HTTP endpoint (/json/list, ...): its JSON, or its text as a JSON string. */
    suspend fun http(path: String, method: String = "GET"): JsonElement

    /** One protocol call on a target's WebSocket (/devtools/...): the reply's result; devtools_failed with its error. */
    suspend fun cdp(path: String, method: String, params: JsonObject): JsonObject
}

/** Chrome DevTools' commands, as the adb client runs them (phone_client.py's cmd_chrome_*). */
object DevTools {
    const val SOCKET = "chrome_devtools_remote"
    private val SOCKET_RE = Regex("^[\\w.-]{1,100}$")
    private val METHOD_RE = Regex("^[A-Za-z]+\\.[A-Za-z]+$")
    private val TAB_ID_RE = Regex("^[\\w-]{1,100}$")

    /** `socket` names another DevTools socket (a debuggable WebView's; /chrome/sockets lists them). */
    fun socket(args: JsonObject): String {
        val v = args["socket"] ?: return SOCKET
        val s = (v as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (s == null || !SOCKET_RE.matches(s)) throw CommandError("invalid_args", "socket must be a DevTools socket name, e.g. chrome_devtools_remote")
        return s
    }

    /** The sockets in /proc/net/unix's listing whose names end as DevTools' do. */
    fun sockets(procNetUnix: String): List<String> = procNetUnix.lines()
        .filter { "@" in it && "devtools_remote" in it }
        .map { it.substringAfter("@").trim() }
        .toSortedSet().toList()

    fun tabList(raw: JsonElement): List<JsonObject> {
        val list = raw as? JsonArray ?: throw CommandError("devtools_failed", "DevTools did not list targets")
        return list.filterIsInstance<JsonObject>().map { t ->
            buildJsonObject { for (k in listOf("id", "type", "title", "url")) put(k, t[k] ?: JsonNull) }
        }
    }

    private fun JsonObject.str(k: String) = (this[k] as? JsonPrimitive)?.takeIf { it.isString }?.content

    /**
     * The WebSocket path of the target named by `tab`: a tab id, an index into the pages, or
     * "browser" for the browser itself. By default the first page (Chrome lists the one in front first).
     */
    suspend fun pickTab(link: DevToolsLink, args: JsonObject): String {
        val tab = args["tab"] ?: JsonPrimitive(0)
        if (tab is JsonPrimitive && tab.isString && tab.content == "browser") {
            // Desktop Chrome gives /devtools/browser/<id>; Chrome on Android plain /devtools/browser.
            val url = (link.http("/json/version") as? JsonObject)?.str("webSocketDebuggerUrl") ?: ""
            if ("/devtools/browser" !in url) throw CommandError("devtools_failed", "no browser target")
            return url.substring(url.indexOf("/devtools/browser"))
        }
        val pages = tabList(link.http("/json/list")).filter { it.str("type") == "page" }
        if (tab is JsonPrimitive && !tab.isString && tab.intOrNull != null && tab.content.toIntOrNull() != null) {
            val i = tab.intOrNull!!
            if (i !in pages.indices) throw CommandError("not_found", "no tab $i: ${pages.size} open (Chrome in the background lists none)")
            return "/devtools/page/${pages[i].str("id")}"
        }
        if (tab is JsonPrimitive && tab.isString && TAB_ID_RE.matches(tab.content)) {
            if (pages.none { it.str("id") == tab.content }) throw CommandError("not_found", "no tab with id ${tab.content}")
            return "/devtools/page/${tab.content}"
        }
        throw CommandError("invalid_args", "tab must be a tab id, an index, or \"browser\"")
    }

    suspend fun tabs(link: DevToolsLink): JsonObject {
        val tabs = tabList(link.http("/json/list"))
        return buildJsonObject { put("count", tabs.size); put("tabs", JsonArray(tabs)) }
    }

    /** JavaScript in a page (Runtime.evaluate): its value as JSON, or the exception it threw. */
    suspend fun eval(link: DevToolsLink, args: JsonObject): JsonObject {
        val expression = args.stringArg("expression")!!
        val awaitPromise = args.boolArg("await_promise", true)
        val result = link.cdp(pickTab(link, args), "Runtime.evaluate", buildJsonObject {
            put("expression", expression); put("returnByValue", true); put("awaitPromise", awaitPromise); put("userGesture", true)
        })
        (result["exceptionDetails"] as? JsonObject)?.let { details ->
            val text = (details["exception"] as? JsonObject)?.str("description") ?: details.str("text") ?: "exception"
            return buildJsonObject { put("threw", true); put("exception", text.take(2000)) }
        }
        val value = result["result"] as? JsonObject ?: JsonObject(emptyMap())
        return buildJsonObject {
            put("threw", false)
            put("type", value["type"] ?: JsonNull)
            put("value", value["value"] ?: JsonNull)
            put("description", value["description"] ?: JsonNull)
        }
    }

    /** Any DevTools protocol method on a tab (or the browser): its result as DevTools returns it. */
    suspend fun cdp(link: DevToolsLink, args: JsonObject): JsonObject {
        val method = args.stringArg("method")!!
        if (!METHOD_RE.matches(method)) throw CommandError("invalid_args", "method must look like Domain.method, e.g. Page.navigate")
        val params = when (val p = args["params"]) {
            null, is JsonNull -> JsonObject(emptyMap())
            is JsonObject -> p
            else -> throw CommandError("invalid_args", "params must be an object")
        }
        return buildJsonObject { put("result", link.cdp(pickTab(link, args), method, params)) }
    }

    /** A new tab, through Target.createTarget on the browser: Chrome on Android refuses /json/new. */
    suspend fun new(link: DevToolsLink, args: JsonObject): JsonObject {
        val url = args.stringArg("url")!!
        val created = link.cdp(pickTab(link, buildJsonObject { put("tab", "browser") }), "Target.createTarget", buildJsonObject { put("url", url) })
        val id = created.str("targetId") ?: throw CommandError("devtools_failed", "no tab created: ${created.toString().take(200)}")
        val listed = tabList(link.http("/json/list")).firstOrNull { it.str("id") == id }
        return buildJsonObject {
            put("tab", listed ?: buildJsonObject { put("id", id); put("type", "page"); put("title", ""); put("url", url) })
        }
    }

    /** /json/close or /json/activate for a tab (not the browser). */
    suspend fun tabAction(link: DevToolsLink, args: JsonObject, action: String): JsonObject {
        val tab = args["tab"]
        if (tab is JsonPrimitive && tab.isString && tab.content == "browser") throw CommandError("invalid_args", "$action takes a tab, not the browser")
        val id = pickTab(link, args).substringAfterLast('/')
        val out = link.http("/json/$action/$id")
        return buildJsonObject {
            put("tab", id)
            put("reply", if (out is JsonPrimitive && out.isString) out.content else out.toString().take(200))
        }
    }
}
