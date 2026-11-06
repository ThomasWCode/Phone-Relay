package com.thomaswcode.phonerelay.commands

/**
 * Typing without key events: the accessibility framework can only replace a field's whole text
 * (ACTION_SET_TEXT), so typed text is spliced in at the cursor (replacing any selection) and the
 * cursor put after it, as typing would. An unknown selection types at the end. A password field
 * reports no readable text, so typing there replaces its content.
 */
object TextEdit {
    data class Result(val text: String, val cursor: Int)

    fun insert(field: Field, typed: String): Result {
        if (field.password) return Result(typed, typed.length)
        val current = field.text
        var start = field.selectionStart
        var end = field.selectionEnd
        if (start < 0 || end < 0 || start > current.length || end > current.length) {
            start = current.length
            end = current.length
        }
        if (start > end) start = end.also { end = start }
        val text = current.substring(0, start) + typed + current.substring(end)
        return Result(text, start + typed.length)
    }

    /** The field's text with the character before the cursor (or the selection) removed: Backspace. */
    fun deleteBack(field: Field): Result? {
        if (field.password) return null
        val current = field.text
        var start = field.selectionStart.takeIf { it in 0..current.length } ?: current.length
        var end = field.selectionEnd.takeIf { it in 0..current.length } ?: current.length
        if (start > end) start = end.also { end = start }
        if (start == end) {
            if (start == 0) return Result(current, 0)
            start -= Character.charCount(current.codePointBefore(start))
        }
        return Result(current.substring(0, start) + current.substring(end), start)
    }

    /** The field's text with the character after the cursor (or the selection) removed: Delete. */
    fun deleteForward(field: Field): Result? {
        if (field.password) return null
        val current = field.text
        var start = field.selectionStart.takeIf { it in 0..current.length } ?: current.length
        var end = field.selectionEnd.takeIf { it in 0..current.length } ?: current.length
        if (start > end) start = end.also { end = start }
        if (start == end) {
            if (end == current.length) return Result(current, end)
            end += Character.charCount(current.codePointAt(end))
        }
        return Result(current.substring(0, start) + current.substring(end), start)
    }
}
