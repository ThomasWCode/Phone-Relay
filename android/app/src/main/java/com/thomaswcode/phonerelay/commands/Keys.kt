package com.thomaswcode.phonerelay.commands

/**
 * What a key does in the app. Only the shell may inject key events, so each key is done the way an
 * app can: a global action, the keyboard's action key, editing the focused field, scrolling, the
 * volume or a media key. Keys none of these can do are refused as unsupported: the adb client
 * presses any key.
 */
sealed class KeyAction {
    /** A key for the focused text field: pressed there as a key when the app has an input connection. */
    open val editsField: Boolean get() = false

    data class Global(val action: GlobalAction) : KeyAction()
    data object ImeEnter : KeyAction() { override val editsField = true }
    data object DeleteBack : KeyAction() { override val editsField = true }
    data object DeleteForward : KeyAction() { override val editsField = true }
    data class Insert(val text: String) : KeyAction() { override val editsField = true }
    data object CursorHome : KeyAction() { override val editsField = true }
    data object CursorEnd : KeyAction() { override val editsField = true }
    data class Scroll(val forward: Boolean) : KeyAction()
    data class Volume(val direction: Int) : KeyAction()
    data class Media(val keyName: String) : KeyAction()
    data object Wake : KeyAction()
    data class Unsupported(val reason: String) : KeyAction()
}

object Keys {
    private val SHORT = mapOf(
        "back" to "KEYCODE_BACK", "home" to "KEYCODE_HOME", "recents" to "KEYCODE_APP_SWITCH",
        "enter" to "KEYCODE_ENTER", "tab" to "KEYCODE_TAB", "delete" to "KEYCODE_DEL",
    )
    private val RAW = Regex("^KEYCODE_[A-Z0-9_]{1,40}$")

    /** The KEYCODE_* name for a short name or a KEYCODE_* name; key numbers are named by the caller. */
    fun name(key: String): String {
        SHORT[key]?.let { return it }
        if (RAW.matches(key)) return key
        throw CommandError("invalid_args", "key must be one of ${SHORT.keys.joinToString(", ")}, a KEYCODE_* name or a key number")
    }

    fun action(name: String, longpress: Boolean = false): KeyAction {
        letter(name)?.let { return KeyAction.Insert(it) }
        return when (name) {
            "KEYCODE_BACK", "KEYCODE_ESCAPE" -> KeyAction.Global(GlobalAction.BACK)
            "KEYCODE_HOME" -> KeyAction.Global(GlobalAction.HOME)
            "KEYCODE_APP_SWITCH" -> KeyAction.Global(GlobalAction.RECENTS)
            "KEYCODE_NOTIFICATION" -> KeyAction.Global(GlobalAction.NOTIFICATIONS)
            "KEYCODE_POWER" -> KeyAction.Global(if (longpress) GlobalAction.POWER_DIALOG else GlobalAction.LOCK_SCREEN)
            "KEYCODE_SLEEP", "KEYCODE_SOFT_SLEEP" -> KeyAction.Global(GlobalAction.LOCK_SCREEN)
            "KEYCODE_WAKEUP" -> KeyAction.Wake
            "KEYCODE_SYSRQ" -> KeyAction.Global(GlobalAction.SCREENSHOT)
            "KEYCODE_HEADSETHOOK" -> KeyAction.Global(GlobalAction.HEADSET_HOOK)
            "KEYCODE_DPAD_UP" -> KeyAction.Global(GlobalAction.DPAD_UP)
            "KEYCODE_DPAD_DOWN" -> KeyAction.Global(GlobalAction.DPAD_DOWN)
            "KEYCODE_DPAD_LEFT" -> KeyAction.Global(GlobalAction.DPAD_LEFT)
            "KEYCODE_DPAD_RIGHT" -> KeyAction.Global(GlobalAction.DPAD_RIGHT)
            "KEYCODE_DPAD_CENTER" -> KeyAction.Global(GlobalAction.DPAD_CENTER)
            "KEYCODE_ENTER", "KEYCODE_NUMPAD_ENTER" -> KeyAction.ImeEnter
            "KEYCODE_DEL" -> KeyAction.DeleteBack
            "KEYCODE_FORWARD_DEL" -> KeyAction.DeleteForward
            "KEYCODE_TAB" -> KeyAction.Insert("\t")
            "KEYCODE_SPACE" -> KeyAction.Insert(" ")
            "KEYCODE_MOVE_HOME" -> KeyAction.CursorHome
            "KEYCODE_MOVE_END" -> KeyAction.CursorEnd
            "KEYCODE_PAGE_DOWN" -> KeyAction.Scroll(forward = true)
            "KEYCODE_PAGE_UP" -> KeyAction.Scroll(forward = false)
            "KEYCODE_VOLUME_UP" -> KeyAction.Volume(1)
            "KEYCODE_VOLUME_DOWN" -> KeyAction.Volume(-1)
            "KEYCODE_VOLUME_MUTE" -> KeyAction.Volume(101)
            else -> if (name.startsWith("KEYCODE_MEDIA_")) KeyAction.Media(name)
            else KeyAction.Unsupported("$name cannot be pressed by the app (only the adb client injects any key)")
        }
    }

    /** KEYCODE_A..Z, KEYCODE_0..9 and a little punctuation as the character they type. */
    private fun letter(name: String): String? {
        val rest = name.removePrefix("KEYCODE_")
        if (rest.length == 1 && (rest[0] in 'A'..'Z')) return rest.lowercase()
        if (rest.length == 1 && (rest[0] in '0'..'9')) return rest
        return mapOf(
            "COMMA" to ",", "PERIOD" to ".", "MINUS" to "-", "EQUALS" to "=", "SLASH" to "/", "AT" to "@",
            "SEMICOLON" to ";", "APOSTROPHE" to "'", "PLUS" to "+", "STAR" to "*", "POUND" to "#",
        )[rest]
    }
}
