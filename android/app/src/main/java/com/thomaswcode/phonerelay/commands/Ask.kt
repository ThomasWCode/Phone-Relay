package com.thomaswcode.phonerelay.commands

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * A request's queued steps in words, for the owner to read on the card before they accept. Typed
 * text and URLs are shown (cut to TEXT_MAX), so they see what will be typed or opened.
 */
object Ask {
    const val TEXT_MAX = 80

    fun describe(step: JsonObject): String {
        val cmd = (step["cmd"] as? JsonPrimitive)?.content ?: ""
        val a = step["args"] as? JsonObject ?: JsonObject(emptyMap())
        fun s(name: String) = (a[name] as? JsonPrimitive)?.takeIf { it.isString }?.content
        fun n(name: String) = a.number(name)?.let(::fmt) ?: "?"
        val words = when (cmd) {
            "sleep" -> "Pause ${n("ms")} ms"
            "state" -> "Check the phone's state"
            "screenshot" -> "Take a screenshot"
            "ui" -> "Read the screen's contents"
            "tap" -> "Tap at ${n("x")}, ${n("y")}"
            "swipe" -> "Swipe from ${n("x1")}, ${n("y1")} to ${n("x2")}, ${n("y2")}"
            "gesture" -> "${(a["pointers"] as? JsonArray)?.size ?: "?"}-finger gesture"
            "type" -> "Type “${cut(s("text") ?: "")}”"
            "key" -> "Press ${(a["key"] as? JsonPrimitive)?.content ?: "a key"}${if (a.boolFlag("longpress")) " (held)" else ""}"
            "launch" -> "Open ${s("package") ?: "an app"}"
            "open" -> "Open ${cut(s("url") ?: "a link")}${s("package")?.let { " in $it" } ?: ""}"
            "intent" -> "${if (a.boolFlag("broadcast")) "Send a broadcast" else "Start an activity"}${(s("action") ?: s("component"))?.let { ": $it" } ?: ""}" +
                if (a.boolFlag("as_shell")) " (as the shell)" else ""
            "notifications" -> "Read the notifications"
            "statusbar" -> when (s("action")) {
                "notifications" -> "Open the notification shade"
                "settings" -> "Open quick settings"
                else -> "Close the shade"
            }
            "rotation" -> if (s("rotation") == "auto") "Let the screen rotate" else "Lock the rotation to ${n("rotation")}"
            "wait" -> when (s("for")) {
                "foreground" -> "Wait for ${s("package") ?: "another app"} to come to the front"
                "text" -> "Wait for “${cut(s("text") ?: "")}” on the screen"
                "change" -> "Wait for the screen to change"
                else -> "Wait for the screen to settle"
            }
            "files" -> "List ${s("path") ?: "a folder"}"
            "record_start" -> "Start recording the screen"
            "clipboard_get" -> "Read the clipboard"
            "clipboard_set" -> if (a["text"] == null || a["text"] is JsonNull) "Empty the clipboard" else "Put “${cut(s("text") ?: "")}” on the clipboard"
            "chrome_tabs" -> "List Chrome's tabs"
            "chrome_eval" -> "Run a script in a Chrome tab"
            "chrome_cdp" -> "Chrome DevTools: ${s("method") ?: "a call"}"
            "chrome_new" -> "Open ${cut(s("url") ?: "a page")} in a new Chrome tab"
            "chrome_close" -> "Close a Chrome tab"
            "chrome_activate" -> "Switch Chrome tab"
            "install" -> "Install ${s("path") ?: "an APK"} over ${s("package") ?: "an app"}"
            "accessibility" -> "Check the accessibility services"
            "logcat" -> "Read the log"
            "package" -> "Check what is installed of ${s("name") ?: "an app"}"
            "settings" -> "Read the setting ${s("key") ?: ""}".trimEnd()
            "run_as_files" -> "List ${s("package") ?: "an app"}'s own files"
            else -> cmd
        }
        val guard = a.ifForegroundOrNull()
        return if (guard != null) "$words (only in ${guard.joinToString(", ")})" else words
    }

    private fun cut(text: String): String {
        val oneLine = text.replace('\n', ' ').replace('\t', ' ')
        return if (oneLine.length <= TEXT_MAX) oneLine else oneLine.take(TEXT_MAX - 1) + "…"
    }

    private fun JsonObject.boolFlag(name: String) = (this[name] as? JsonPrimitive)?.content == "true"

    private fun JsonObject.ifForegroundOrNull(): List<String>? = runCatching { ifForeground() }.getOrNull()
}
