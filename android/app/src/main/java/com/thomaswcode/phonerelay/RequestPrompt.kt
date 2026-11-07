package com.thomaswcode.phonerelay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.util.Log
import com.thomaswcode.phonerelay.commands.Answer
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Asking the owner whether the agent may use the phone (the relay's /request): the card over the
 * screen (RequestCard), and a notification with the same answers for when the card is out of sight
 * (the screen was off, the card dragged away). Whichever answers first wins and both go. One request
 * at a time, as the relay's commands run.
 */
object RequestPrompt {
    private const val TAG = "PhoneRelay"
    private const val CHANNEL = "requests"
    private const val NOTIFICATION_ID = 3
    const val ACTION_ANSWER = "com.thomaswcode.phonerelay.ANSWER"
    const val EXTRA_TOKEN = "token"
    const val EXTRA_ACCEPT = "accept"
    const val KEY_MESSAGE = "message"
    /** After the card goes, before a queued step's first touch: a frame or two for the screen to lose it. */
    private const val CLEAR_MS = 300L

    private val tokens = AtomicInteger()
    @Volatile private var current: Pair<Int, CompletableDeferred<Answer>>? = null

    /** An answer from the card or the notification to request `token`; one for an earlier request is dropped. */
    fun answer(token: Int, answer: Answer) {
        val c = current ?: return
        if (c.first == token) c.second.complete(answer)
    }

    suspend fun ask(context: Context, reason: String, steps: List<String>, seconds: Double): Answer {
        val token = tokens.incrementAndGet()
        val pending = CompletableDeferred<Answer>()
        current = token to pending
        val ms = (seconds * 1000).toLong().coerceAtLeast(1)
        var card: RequestCard? = null
        val answer = try {
            val service = PhoneAccessibilityService.instance
            if (service != null) {
                card = withContext(Dispatchers.Main) {
                    runCatching { RequestCard(service, reason, steps, SystemClock.elapsedRealtime() + ms) { answer(token, it) }.also { it.show() } }
                        .onFailure { Log.w(TAG, "could not show the request card", it) }
                        .getOrNull()
                }
            }
            notify(context, token, reason, steps, ms)
            withTimeoutOrNull(ms) { pending.await() } ?: Answer(null, null)
        } finally {
            // The connection closing (the owner's switch, a drop) cancels the wait: the card goes with it.
            current = null
            withContext(NonCancellable + Dispatchers.Main) { card?.dismiss() }
            runCatching { manager(context).cancel(NOTIFICATION_ID) }
        }
        if (answer.accepted == true) delay(CLEAR_MS)
        return answer
    }

    private fun manager(context: Context): NotificationManager {
        val manager = context.getSystemService(NotificationManager::class.java)
        // Default importance: a sound and the status bar, but no heads-up over the card.
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, context.getString(R.string.request_channel), NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = context.getString(R.string.request_channel_description)
                setShowBadge(false)
            },
        )
        return manager
    }

    private fun notify(context: Context, token: Int, reason: String, steps: List<String>, ms: Long) {
        fun answerIntent(code: Int, accept: Boolean, mutable: Boolean) = PendingIntent.getBroadcast(
            context, code,
            Intent(context, RequestReceiver::class.java).setAction(ACTION_ANSWER).putExtra(EXTRA_TOKEN, token).putExtra(EXTRA_ACCEPT, accept),
            // Mutable only for the reply field, which fills the intent in.
            (if (mutable) PendingIntent.FLAG_MUTABLE else PendingIntent.FLAG_IMMUTABLE) or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val text = if (steps.isEmpty()) reason else reason + "\n\n" + context.getString(R.string.request_then) + "\n" + steps.joinToString("\n") { "• $it" }
        val input = RemoteInput.Builder(KEY_MESSAGE).setLabel(context.getString(R.string.request_reason)).build()
        val notification = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_relay)
            .setContentTitle(context.getString(R.string.request_title))
            .setContentText(reason)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setOngoing(true)
            .setTimeoutAfter(ms)
            .setCategory(Notification.CATEGORY_STATUS)
            .addAction(Notification.Action.Builder(null, context.getString(R.string.request_accept), answerIntent(10, accept = true, mutable = false)).build())
            .addAction(Notification.Action.Builder(null, context.getString(R.string.request_deny), answerIntent(11, accept = false, mutable = false)).build())
            .addAction(
                Notification.Action.Builder(null, context.getString(R.string.request_deny_reason), answerIntent(12, accept = false, mutable = true))
                    .addRemoteInput(input).build(),
            )
            .build()
        runCatching { manager(context).notify(NOTIFICATION_ID, notification) }
            .onFailure { Log.w(TAG, "could not post the request notification", it) }
    }
}

/** An answer given on the request's notification (Accept, Deny, or Deny with a reason typed into it). */
class RequestReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != RequestPrompt.ACTION_ANSWER) return
        val message = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(RequestPrompt.KEY_MESSAGE)?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        RequestPrompt.answer(
            intent.getIntExtra(RequestPrompt.EXTRA_TOKEN, -1),
            Answer(intent.getBooleanExtra(RequestPrompt.EXTRA_ACCEPT, false), message),
        )
    }
}
