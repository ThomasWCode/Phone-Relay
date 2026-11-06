package com.thomaswcode.phonerelay

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews

/**
 * The home screen widget: one button, the owner's on/off switch for the relay, as the tile. It shows
 * whether the relay is on and, while it is, how the connection stands. Redrawn by the app when either
 * changes (RelayApp.setEnabled, RelayService), so it asks Android for no periodic updates.
 */
class ToggleWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, appWidgetManager: AppWidgetManager, appWidgetIds: IntArray) {
        appWidgetManager.updateAppWidget(appWidgetIds, views(context))
    }

    companion object {
        /** Every placed widget redrawn with the relay's state now. */
        fun refresh(context: Context) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, ToggleWidget::class.java))
            if (ids.isNotEmpty()) manager.updateAppWidget(ids, views(context))
        }

        private fun views(context: Context): RemoteViews {
            val app = RelayApp.of(context)
            val on = app.prefs.enabled
            val state = if (on) shortLabel(app.connection.status.value) else context.getString(R.string.off)
            // A tap opens ToggleActivity, a moment in front: the relay's foreground service may always
            // be started from an activity, and not always from a widget's broadcast.
            val toggle = PendingIntent.getActivity(
                context, 0,
                Intent(context, ToggleActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION),
                PendingIntent.FLAG_IMMUTABLE,
            )
            return RemoteViews(context.packageName, R.layout.widget_toggle).apply {
                setInt(android.R.id.background, "setBackgroundResource", if (on) R.drawable.widget_on else R.drawable.widget_off)
                setTextViewText(R.id.widget_state, state)
                setContentDescription(android.R.id.background, context.getString(R.string.widget_state, state))
                setOnClickPendingIntent(android.R.id.background, toggle)
            }
        }

        /** The status in a word or two: a 1x1 widget has room for little more. */
        private fun shortLabel(status: Status) = when (status) {
            Status.Off -> "Off"
            Status.NoSettings -> "Not set up"
            Status.Connecting -> "Connecting"
            is Status.Connected -> "Connected"
            is Status.Waiting -> "Reconnecting"
        }
    }
}
