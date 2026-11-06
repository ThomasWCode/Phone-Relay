package com.thomaswcode.phonerelay.commands

/**
 * Typing through the shell, as the adb client does it (phone_client.py's type_commands): `input
 * text` per line of ASCII, Enter for a newline, Tab for a tab. For the app's shell bridge, when no
 * text field has focus.
 */
object ShellText {
    /** Characters per `input text` call, so a long text never becomes an over-long shell command. */
    const val CHUNK = 1000

    /** Whether `input text` can type it: ASCII, with newlines and tabs as the only control characters. */
    fun canType(text: String): Boolean = text.isNotEmpty() && text.all { it.code in 0x20..0x7E || it in "\n\r\t" }

    /** Single-quoted for the shell, spaces as %s (input's own escape); one line of printable ASCII. */
    fun escape(text: String): String {
        require(text.all { it.code in 0x20..0x7E }) { "one `input text` call takes one line of printable ASCII" }
        return "'" + text.replace(" ", "%s").replace("'", "'\\''") + "'"
    }

    /** One line typed exactly, '%' included: `input text` turns "%s" into a space, so a literal "%s" goes as "%" then "s...". */
    fun inputText(line: String): String {
        val parts = line.split("%s")
        val segments = mutableListOf(parts[0])
        for (part in parts.drop(1)) {
            segments[segments.lastIndex] = segments.last() + "%"
            segments += "s$part"
        }
        return segments.filter { it.isNotEmpty() }.joinToString(" && ") { "input text ${escape(it)}" }
    }

    /** Shell commands typing `text` exactly, joined with && while that stays short. */
    fun commands(text: String): List<String> {
        require(canType(text)) { "only ASCII can be typed through the shell" }
        val normal = text.replace("\r\n", "\n").replace('\r', '\n')
        val steps = mutableListOf<String>()
        for (piece in splitKeeping(normal)) {
            when (piece) {
                "\n" -> steps += "input keyevent KEYCODE_ENTER"
                "\t" -> steps += "input keyevent KEYCODE_TAB"
                else -> piece.chunked(CHUNK).forEach { steps += inputText(it) }
            }
        }
        val out = mutableListOf<String>()
        for (step in steps) {
            if (out.isNotEmpty() && out.last().length + step.length < 4 * CHUNK) out[out.lastIndex] = out.last() + " && " + step
            else out += step
        }
        return out
    }

    /** `text` split around each newline and tab, keeping them as pieces of their own. */
    private fun splitKeeping(text: String): List<String> {
        val pieces = mutableListOf<String>()
        val current = StringBuilder()
        for (c in text) {
            if (c == '\n' || c == '\t') {
                if (current.isNotEmpty()) pieces += current.toString().also { current.clear() }
                pieces += c.toString()
            } else current.append(c)
        }
        if (current.isNotEmpty()) pieces += current.toString()
        return pieces
    }
}
