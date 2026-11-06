package com.thomaswcode.phonerelay

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import java.lang.ref.WeakReference

/**
 * Asks Android for a screen or sound capture: its consent prompt (which the accessibility service
 * answers) needs an activity to answer to. Invisible, and in front only until the answer comes,
 * when Capture starts the capture: Android lets a projection begin while its app is in front.
 */
class ProjectionActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        invisible(window)
        setShowWhenLocked(true) // the agent may try with the phone locked; Android decides what it allows
        overrideActivityTransition(OVERRIDE_TRANSITION_OPEN, 0, 0)
        overrideActivityTransition(OVERRIDE_TRANSITION_CLOSE, 0, 0)
        current = WeakReference(this)
        if (savedInstanceState != null) return // recreated: the prompt's answer still comes here
        val projections = getSystemService(MediaProjectionManager::class.java)
        // The whole screen, as screenrecord: no choice of a single app in the prompt.
        @Suppress("DEPRECATION")
        startActivityForResult(projections.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay()), REQUEST)
    }

    @Deprecated("Activity's own result callback; this app has no AndroidX activity here")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQUEST) RelayApp.of(this).capture.consented(resultCode, data)
        finish()
    }

    companion object {
        private const val REQUEST = 1
        @Volatile
        private var current: WeakReference<ProjectionActivity>? = null

        /** Closes this window and the prompt it asked for, if either is still open (a capture given up on). */
        fun close() {
            Handler(Looper.getMainLooper()).post {
                current?.get()?.takeIf { !it.isFinishing }?.let {
                    it.finishActivity(REQUEST)
                    it.finish()
                }
            }
        }
    }
}
