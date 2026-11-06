package com.thomaswcode.phonerelay.commands

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * The UI tree as the relay's elements (the adb client's parse_ui shape): every visible node from
 * every window, with text, description, short id and class, package, bounds, centre and flags.
 * Nodes with nothing to read or act on are dropped; so is anything not visible, with its children.
 */
object UiTree {
    fun elements(roots: List<UiNode>): List<JsonObject> {
        val out = mutableListOf<JsonObject>()
        for (root in roots) walk(root, out)
        return out
    }

    private fun walk(node: UiNode, out: MutableList<JsonObject>) {
        if (!node.visible) return
        element(node, out.size)?.let { out += it }
        for (child in node.children) walk(child, out)
    }

    private fun element(n: UiNode, index: Int): JsonObject? {
        val b = n.bounds
        if (b.size != 4 || b[2] <= b[0] || b[3] <= b[1]) return null
        val text = n.text.orEmpty()
        val desc = n.desc.orEmpty()
        if (text.isEmpty() && desc.isEmpty() && !n.clickable && !n.scrollable && !n.editable) return null
        return buildJsonObject {
            put("i", index)
            put("text", text)
            put("desc", desc)
            put("id", short(n.viewId))
            put("cls", short(n.className))
            put("pkg", n.pkg)
            put("bounds", JsonArray(b.map { JsonPrimitive(it) }))
            put("center", JsonArray(listOf(JsonPrimitive((b[0] + b[2]) / 2), JsonPrimitive((b[1] + b[3]) / 2))))
            for ((flag, value) in listOf(
                "clickable" to n.clickable, "scrollable" to n.scrollable, "editable" to n.editable,
                "focused" to n.focused, "password" to n.password, "checked" to n.checked,
            )) if (value) put(flag, true)
        }
    }

    /** com.app:id/name -> name; android.widget.TextView -> TextView. */
    fun short(name: String?): String = name.orEmpty().substringAfterLast('/').substringAfterLast('.')

    /** Whether an element's text or description contains `wanted` (for /wait). */
    fun contains(element: JsonObject, wanted: String): Boolean =
        listOf("text", "desc").any { (element[it] as? JsonPrimitive)?.content?.contains(wanted) == true }
}
