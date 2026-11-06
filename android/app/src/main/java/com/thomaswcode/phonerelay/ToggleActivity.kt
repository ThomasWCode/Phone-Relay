package com.thomaswcode.phonerelay

import android.app.Activity
import android.os.Bundle

/**
 * The home screen widget's tap: a moment in front, with nothing to see, to switch the relay on or off.
 * From an activity in front, Android always lets the app start its foreground service.
 */
class ToggleActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = RelayApp.of(this)
        app.setEnabled(!app.prefs.enabled)
        finish()
    }
}
