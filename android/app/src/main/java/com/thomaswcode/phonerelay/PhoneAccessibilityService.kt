package com.thomaswcode.phonerelay

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.util.Log
import android.view.accessibility.AccessibilityEvent

/**
 * The app's hands and eyes: gestures, global actions, the UI tree, screenshots and text fields
 * (AndroidDevice drives it). Android binds it at boot and keeps it bound, so it also brings the
 * relay back: an app with a bound accessibility service may start its foreground service from
 * the background.
 *
 * Nothing here may throw while connecting: Android silently drops an accessibility service that
 * crashes as it connects from the enabled list.
 */
class PhoneAccessibilityService : AccessibilityService() {
    override fun onServiceConnected() {
        instance = this
        runCatching { RelayService.startIfEnabled(this) }
            .onFailure { Log.w(TAG, "could not start the relay from the accessibility service", it) }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "PhoneRelay"

        @Volatile
        var instance: PhoneAccessibilityService? = null
            private set
    }
}
