package com.thomaswcode.phonerelay

import android.app.Application
import android.content.ComponentName
import android.service.quicksettings.TileService
import android.view.KeyEvent
import com.thomaswcode.phonerelay.commands.Commands
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob

/** The app's one relay connection, its settings, and the owner's on/off switch. */
class RelayApp : Application() {
    val scope = CoroutineScope(SupervisorJob())
    lateinit var prefs: Prefs
        private set
    lateinit var connection: RelayConnection
        private set

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        prefs.importPending()
        val commands = Commands(AndroidDevice(this), BuildConfig.VERSION_NAME) { code ->
            KeyEvent.keyCodeToString(code).takeIf { it.startsWith("KEYCODE_") }
        }
        connection = RelayConnection(prefs, commands, BuildConfig.VERSION_NAME, scope)
    }

    /** The owner's switch (the app's screen, the Quick Settings tile, the notification): kept across reboots. */
    fun setEnabled(on: Boolean) {
        prefs.enabled = on
        if (on) RelayService.start(this) else RelayService.stop(this)
        TileService.requestListeningState(this, ComponentName(this, ToggleTile::class.java))
    }

    companion object {
        fun of(context: android.content.Context) = context.applicationContext as RelayApp
    }
}
