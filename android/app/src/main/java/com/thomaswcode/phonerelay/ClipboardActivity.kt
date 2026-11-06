package com.thomaswcode.phonerelay

import android.app.Activity
import android.content.ClipboardManager
import android.os.Bundle
import android.view.Gravity
import android.view.Window
import android.view.WindowManager
import kotlinx.coroutines.CompletableDeferred

/**
 * /clipboard's reader: Android lets only the app in front (or the keyboard) read the clipboard, so
 * this invisible window comes forward, reads it once it has focus, and closes again. Started with
 * FLAG_ACTIVITY_NO_USER_ACTION, so a video playing behind it does not go picture-in-picture.
 */
class ClipboardActivity : Activity() {
    /** The read this window was opened for: a later one is not answered by an earlier window. */
    private var mine: CompletableDeferred<String?>? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        mine = pending
        invisible(window)
        setShowWhenLocked(true) // the agent may try with the phone locked; Android decides what it allows
        overrideActivityTransition(OVERRIDE_TRANSITION_OPEN, 0, 0)
        overrideActivityTransition(OVERRIDE_TRANSITION_CLOSE, 0, 0)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus) return
        val clip = runCatching { getSystemService(ClipboardManager::class.java).primaryClip }.getOrNull()
        val text = clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        mine?.complete(text)
        finish()
    }

    companion object {
        /** The command waiting for the clipboard's text. */
        @Volatile
        var pending: CompletableDeferred<String?>? = null
    }
}

/** A window of one pixel in the corner, letting touches through to the app behind: nothing shows. */
internal fun invisible(window: Window) {
    window.setLayout(1, 1)
    window.setGravity(Gravity.TOP or Gravity.START)
    window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL)
}
