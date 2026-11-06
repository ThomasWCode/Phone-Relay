package com.thomaswcode.phonerelay

import android.app.Application
import android.content.ComponentName
import android.service.quicksettings.TileService
import android.view.KeyEvent
import com.thomaswcode.phonerelay.commands.Commands
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** The app's one relay connection, its settings, and the owner's on/off switch. */
class RelayApp : Application() {
    val scope = CoroutineScope(SupervisorJob())
    lateinit var prefs: Prefs
        private set
    lateinit var connection: RelayConnection
        private set
    /** Screen recording and sound capture: ProjectionActivity hands it Android's consent. */
    lateinit var capture: Capture
        private set
    /** The shell bridge: the app's own adb connection to this phone, paired by the owner (Pairing). */
    lateinit var shell: AdbShell
        private set
    private lateinit var _enabled: MutableStateFlow<Boolean>
    /** Whether the owner has the relay on, wherever it was switched (the app, the tile, the notification). */
    val enabled: StateFlow<Boolean> get() = _enabled

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        _enabled = MutableStateFlow(prefs.enabled)
        prefs.importPending()
        capture = Capture(this, scope)
        shell = AdbShell(this)
        val commands = Commands(AndroidDevice(this, capture, shell, BuildConfig.VERSION_NAME), BuildConfig.VERSION_NAME) { code ->
            KeyEvent.keyCodeToString(code).takeIf { it.startsWith("KEYCODE_") }
        }
        connection = RelayConnection(prefs, commands, BuildConfig.VERSION_NAME, scope)
    }

    /** The owner's switch (the app's screen, the Quick Settings tile, the notification): kept across reboots. */
    fun setEnabled(on: Boolean) {
        prefs.enabled = on
        _enabled.value = on
        if (on) RelayService.start(this) else RelayService.stop(this)
        TileService.requestListeningState(this, ComponentName(this, ToggleTile::class.java))
    }

    companion object {
        fun of(context: android.content.Context) = context.applicationContext as RelayApp
    }
}
