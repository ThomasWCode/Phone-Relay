package com.thomaswcode.phonerelay

import android.Manifest
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/** The owner's screen: the relay's switch and state, and each grant the app needs, with a way to give it. */
class MainActivity : ComponentActivity() {
    private val tick = mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        RelayApp.of(this).prefs.importPending()
        setContent {
            val context = LocalContext.current
            val scheme = if (isSystemInDarkTheme()) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
            MaterialTheme(colorScheme = scheme) {
                Surface(Modifier.fillMaxSize()) { Screen(tick.intValue) }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        tick.intValue++ // grants may have changed in Settings
    }
}

/** A grant: a Settings screen to open, or (open gives null) a runtime permission to ask for. */
private class Grant(
    val name: String,
    val why: String,
    val granted: Boolean,
    val permission: String = Manifest.permission.POST_NOTIFICATIONS,
    val open: () -> Intent?,
)

private fun grants(context: Context): List<Grant> {
    val pkg = Uri.parse("package:${context.packageName}")
    val power = context.getSystemService(PowerManager::class.java)
    val notifications = context.getSystemService(NotificationManager::class.java)
    return listOf(
        Grant("Accessibility service", "touch, type, read the screen, screenshots", PhoneAccessibilityService.instance != null) {
            Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
        },
        Grant("Notifications", "the relay's ongoing notification",
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) { null },
        Grant("Not battery-optimised", "stays connected while the screen is off", power.isIgnoringBatteryOptimizations(context.packageName)) {
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, pkg)
        },
        Grant("Notification access", "/notifications",
            notifications.isNotificationListenerAccessGranted(ComponentName(context, NotificationReader::class.java))) {
            Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
        },
        Grant("All files access", "/files, pull and push", Environment.isExternalStorageManager()) {
            Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, pkg)
        },
        Grant("Modify system settings", "/rotation", Settings.System.canWrite(context)) {
            Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS, pkg)
        },
        Grant("Microphone", "/audio (both sources: Android needs it for the phone's sound too)",
            context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED,
            Manifest.permission.RECORD_AUDIO) { null },
    )
}

@Composable
private fun Screen(@Suppress("UNUSED_PARAMETER") tick: Int) {
    val context = LocalContext.current
    val app = RelayApp.of(context)
    val status by app.connection.status.collectAsStateWithLifecycle()
    // Shared state: the tile, the widget and the notification switch it too, while this screen is open.
    val enabled by app.enabled.collectAsStateWithLifecycle()
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }

    Column(
        Modifier.fillMaxSize().safeDrawingPadding().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Phone Relay", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.SemiBold)
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Relay", style = MaterialTheme.typography.titleMedium)
                        Text(if (enabled) status.label else "Off: the agent cannot reach the phone", style = MaterialTheme.typography.bodyMedium)
                    }
                    Switch(checked = enabled, onCheckedChange = { on ->
                        if (on) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                        app.setEnabled(on)
                    })
                }
                Text(
                    "Also from the Quick Settings tile, the home screen widget and the notification. It stays as you leave it after a reboot.",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("Relay settings", style = MaterialTheme.typography.titleMedium)
                val url = app.prefs.relayUrl
                Text(if (url != null) "Relay: ${Uri.parse(url.replaceFirst("wss://", "https://")).host}" else "Relay URL: not set")
                Text(if (app.prefs.hasCredentials()) "Device token: set (encrypted)" else "Device token: not set")
                Text("Both arrive over adb from the owner's computer; see the repository's README.", style = MaterialTheme.typography.bodySmall)
            }
        }
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Shell access", style = MaterialTheme.typography.titleMedium)
                Text(
                    if (app.shell.paired) "✓ Paired with wireless debugging" else "✗ Not paired",
                    style = MaterialTheme.typography.titleSmall,
                )
                Text(
                    "For Chrome DevTools, keys no app can press, and typing with no text field: the app's own connection to " +
                        "this phone's wireless debugging (Wi-Fi only). Pair once: tap Pair, then in Wireless debugging tap " +
                        "Pair device with pairing code, and type the code into the notification.",
                    style = MaterialTheme.typography.bodySmall,
                )
                Text(
                    if (app.shell.canEnable()) "It switches wireless debugging back on by itself when Android turns it off."
                    else "To have it switch wireless debugging back on by itself (after a reboot): adb shell pm grant " +
                        "${context.packageName} android.permission.WRITE_SECURE_SETTINGS",
                    style = MaterialTheme.typography.bodySmall,
                )
                OutlinedButton(onClick = {
                    Pairing.ask(context)
                    runCatching { context.startActivity(Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)) }
                }) { Text(if (app.shell.paired) "Pair again" else "Pair") }
            }
        }
        Text("What the app needs", style = MaterialTheme.typography.titleMedium)
        for (grant in grants(context)) {
            Card(Modifier.fillMaxWidth()) {
                Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text((if (grant.granted) "✓ " else "✗ ") + grant.name, style = MaterialTheme.typography.titleSmall)
                        Text(grant.why, style = MaterialTheme.typography.bodySmall)
                    }
                    Spacer(Modifier.width(8.dp))
                    if (!grant.granted) {
                        OutlinedButton(onClick = {
                            val intent = grant.open()
                            if (intent == null) askNotifications.launch(grant.permission)
                            else runCatching { context.startActivity(intent) }
                        }) { Text("Allow") }
                    }
                }
            }
        }
    }
}
