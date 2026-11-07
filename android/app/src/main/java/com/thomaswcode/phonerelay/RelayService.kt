package com.thomaswcode.phonerelay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.util.Log
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * The foreground service that keeps the relay connected while the owner has it on: Android keeps
 * a foreground service's process and its network. Type specialUse, which Android 15+ still lets a
 * boot receiver start. Its notification says how the connection stands and has a Turn off button.
 */
class RelayService : Service() {
    private var statusJob: Job? = null
    /** What a capture is doing, for the notification, while one runs. */
    @Volatile private var capturing: String? = null
    private val screen = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) = RelayApp.of(context).connection.nudge()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        val app = RelayApp.of(this)
        channel()
        startForeground(NOTIFICATION_ID, notification(app.connection.status.value.label), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        registerReceiver(screen, IntentFilter(Intent.ACTION_SCREEN_ON).apply { addAction(Intent.ACTION_USER_PRESENT) })
        app.connection.start()
        statusJob = app.scope.launch {
            app.connection.status.collectLatest { status ->
                getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(status.label))
                ToggleWidget.refresh(this@RelayService)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_TURN_OFF) {
            RelayApp.of(this).setEnabled(false)
            return START_NOT_STICKY
        }
        if (!RelayApp.of(this).prefs.enabled) {
            stopSelf()
            return START_NOT_STICKY
        }
        RelayApp.of(this).connection.start()
        return START_STICKY
    }

    override fun onDestroy() {
        instance = null
        statusJob?.cancel()
        runCatching { unregisterReceiver(screen) }
        RelayApp.of(this).connection.stop()
        super.onDestroy()
    }

    /**
     * Takes on a capture's foreground service types (mediaProjection, microphone) beside specialUse,
     * or drops them with 0: Android lets a projection and the microphone run only in a service of
     * their type. The notification says what is being captured.
     */
    fun captureTypes(types: Int, what: String?) {
        capturing = if (types == 0) null else what
        val status = RelayApp.of(this).connection.status.value.label
        startForeground(NOTIFICATION_ID, notification(status), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE or types)
    }

    private fun channel() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW).apply {
                description = getString(R.string.channel_description)
                setShowBadge(false)
            },
        )
    }

    private fun notification(status: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val off = PendingIntent.getService(
            this, 1, Intent(this, RelayService::class.java).setAction(ACTION_TURN_OFF), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_relay)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(capturing?.let { "$status; $it" } ?: status)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, getString(R.string.turn_off), off).build())
            .build()
    }

    companion object {
        private const val TAG = "PhoneRelay"
        private const val CHANNEL = "relay"
        private const val NOTIFICATION_ID = 1
        const val ACTION_TURN_OFF = "com.thomaswcode.phonerelay.TURN_OFF"

        /** The running service, for a capture to take on its types. */
        @Volatile
        var instance: RelayService? = null
            private set

        fun start(context: Context) {
            context.startForegroundService(Intent(context, RelayService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, RelayService::class.java))
        }

        /** From boot, an app update or the accessibility service binding: on again if the owner left it on. */
        fun startIfEnabled(context: Context) {
            if (!RelayApp.of(context).prefs.enabled) return
            runCatching { start(context) }.onFailure { Log.w(TAG, "could not start the relay service", it) }
        }
    }
}
