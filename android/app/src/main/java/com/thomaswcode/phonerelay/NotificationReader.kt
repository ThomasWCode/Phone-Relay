package com.thomaswcode.phonerelay

import android.app.Notification
import android.service.notification.NotificationListenerService
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** Reads the active notifications for /notifications, once the owner grants notification access. */
class NotificationReader : NotificationListenerService() {
    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        instance = null
    }

    /** Each notification as the adb client reports it: key, package, id, tag, title, text, ... */
    fun active(): List<JsonObject> = activeNotifications.orEmpty().map { sbn ->
        val extras = sbn.notification.extras
        fun text(name: String) = extras.getCharSequence(name)?.toString()
        buildJsonObject {
            put("key", sbn.key)
            put("pkg", sbn.packageName)
            put("id", sbn.id)
            put("tag", sbn.tag)
            put("posted", sbn.postTime)
            for ((field, extra) in listOf(
                "title" to Notification.EXTRA_TITLE, "text" to Notification.EXTRA_TEXT, "big_text" to Notification.EXTRA_BIG_TEXT,
                "sub_text" to Notification.EXTRA_SUB_TEXT, "info_text" to Notification.EXTRA_INFO_TEXT,
                "summary" to Notification.EXTRA_SUMMARY_TEXT, "conversation" to Notification.EXTRA_CONVERSATION_TITLE,
            )) text(extra)?.let { put(field, it) }
            extras.getString(Notification.EXTRA_TEMPLATE)?.let { put("template", it) }
        }
    }

    companion object {
        @Volatile
        var instance: NotificationReader? = null
            private set
    }
}
