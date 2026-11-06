package com.thomaswcode.phonerelay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.thomaswcode.phonerelay.commands.CommandError
import kotlinx.coroutines.launch

/**
 * Pairing the shell bridge on the phone alone, as Shizuku does: the owner opens Settings >
 * Developer options > Wireless debugging > Pair device with pairing code, pulls down the shade and
 * types the code into this notification's reply field (the code stays showing behind the shade).
 * PairingReceiver takes the reply and pairs.
 */
object Pairing {
    private const val CHANNEL = "pairing"
    private const val NOTIFICATION_ID = 2
    const val KEY_CODE = "code"
    const val ACTION_PAIR = "com.thomaswcode.phonerelay.PAIR"

    private fun manager(context: Context): NotificationManager {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, context.getString(R.string.pairing_channel), NotificationManager.IMPORTANCE_HIGH).apply {
                setShowBadge(false)
            },
        )
        return manager
    }

    /** The notification asking for the code, with `text` saying what to do (or what went wrong). */
    fun ask(context: Context, text: String = context.getString(R.string.pairing_how)) {
        val reply = PendingIntent.getBroadcast(
            context, 3, Intent(context, PairingReceiver::class.java).setAction(ACTION_PAIR),
            PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT, // mutable: the reply field fills it in
        )
        val input = RemoteInput.Builder(KEY_CODE).setLabel(context.getString(R.string.pairing_code)).build()
        val notification = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_relay)
            .setContentTitle(context.getString(R.string.pairing_title))
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setOnlyAlertOnce(true)
            .addAction(Notification.Action.Builder(null, context.getString(R.string.pairing_code), reply).addRemoteInput(input).build())
            .build()
        manager(context).notify(NOTIFICATION_ID, notification)
    }

    /** The outcome, in place of the question. */
    fun done(context: Context, text: String) {
        val notification = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_relay)
            .setContentTitle(context.getString(R.string.pairing_title))
            .setContentText(text)
            .setTimeoutAfter(60_000)
            .build()
        manager(context).notify(NOTIFICATION_ID, notification)
    }
}

/** The pairing code typed into Pairing's notification. */
class PairingReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Pairing.ACTION_PAIR) return
        val code = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(Pairing.KEY_CODE)?.toString()?.filter { it.isDigit() }
        if (code.isNullOrEmpty()) {
            Pairing.ask(context, context.getString(R.string.pairing_digits))
            return
        }
        val app = RelayApp.of(context)
        val pending = goAsync()
        app.scope.launch {
            try {
                app.shell.pair(code)
                Pairing.done(context, context.getString(R.string.pairing_done))
            } catch (e: CommandError) {
                Pairing.ask(context, e.detail)
            } catch (e: Exception) {
                Pairing.ask(context, "Pairing failed: ${e.javaClass.simpleName}")
            } finally {
                pending.finish()
            }
        }
    }
}
