package com.thomaswcode.phonerelay

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** After a reboot or an update of the app: the relay on again if the owner left it on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) {
            RelayService.startIfEnabled(context)
        }
    }
}
