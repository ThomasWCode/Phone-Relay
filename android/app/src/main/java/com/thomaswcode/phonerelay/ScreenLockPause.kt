package com.thomaswcode.phonerelay

import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

// ScreenLock (com.thomaswcode.screenlock, the owner's touch lock) blocks a finger on the screen by taking in the
// touchscreen's events in its accessibility service. Android's gesture injector sits before that, so this app's
// gestures would be taken too: ScreenLock lets this app (checked by its signing certificate) pause the lock around
// each gesture. Without ScreenLock installed, or with it unlocked, the calls do nothing.
class ScreenLockPause(private val context: Context) {

    companion object {
        private val URI: Uri = Uri.parse("content://com.thomaswcode.screenlock.pause")
        // How long past a gesture's own time a pause may last if the resume never comes (ScreenLock caps it at 65 s)
        const val MARGIN_MS = 1_000L
    }

    // Lets the touchscreen through for up to ms (renewing a pause already running)
    suspend fun pause(ms: Long) = call("pause", ms + MARGIN_MS)

    suspend fun resume() = call("resume", null)

    private suspend fun call(method: String, ms: Long?) = withContext(Dispatchers.IO) {
        try {
            context.contentResolver.call(URI, method, null, Bundle().apply { if (ms != null) putLong("ms", ms) })
        } catch (_: IllegalArgumentException) {
            // ScreenLock is not installed (an unknown authority)
        } catch (e: SecurityException) {
            Log.w("PhoneRelay", "ScreenLock refused to pause: ${e.message}")
        }
    }
}
