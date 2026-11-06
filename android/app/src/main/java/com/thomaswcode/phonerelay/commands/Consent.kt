package com.thomaswcode.phonerelay.commands

/**
 * Android's consent prompt before each screen or sound capture (SystemUI's "Start recording or
 * casting with Phone Relay?"), which the accessibility service answers. Only a System UI window
 * that names the app is taken for it, and only its start button is pressed.
 */
object Consent {
    const val SYSTEM_UI = "com.android.systemui"

    /** The start button's words, lower case, across Android and One UI versions. */
    private val START_WORDS = setOf("start", "start now", "share screen", "start recording", "start sharing")
    private val START_IDS = listOf(":id/button_start", ":id/button1")

    /** The prompt's start button in a System UI window's tree, or null if this is not the prompt. */
    fun startButton(root: UiNode, appLabel: String): UiNode? {
        if (root.pkg != SYSTEM_UI) return null
        val nodes = flatten(root)
        if (nodes.none { (it.text ?: "").contains(appLabel) || (it.desc ?: "").contains(appLabel) }) return null
        return nodes.firstOrNull { n -> n.clickable && n.visible && (label(n) in START_WORDS || START_IDS.any { n.viewId?.endsWith(it) == true }) }
    }

    /** A button's words: its own text, else its description, else its first labelled child's. */
    private fun label(n: UiNode): String {
        val own = n.text?.takeIf { it.isNotBlank() } ?: n.desc?.takeIf { it.isNotBlank() }
        val words = own ?: flatten(n).drop(1).firstNotNullOfOrNull { c -> c.text?.takeIf { it.isNotBlank() } }
        return (words ?: "").trim().lowercase()
    }

    private fun flatten(root: UiNode): List<UiNode> {
        val out = mutableListOf<UiNode>()
        val stack = ArrayDeque(listOf(root))
        while (stack.isNotEmpty()) {
            val n = stack.removeLast()
            out += n
            stack.addAll(n.children.asReversed())
        }
        return out
    }
}
