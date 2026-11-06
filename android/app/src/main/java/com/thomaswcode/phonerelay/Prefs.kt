package com.thomaswcode.phonerelay

import android.content.Context
import android.util.Log
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * The app's settings: whether the relay is on (the owner's toggle, kept across reboots), the
 * relay's URL, and the device token, sealed by KeystoreCipher.
 *
 * The URL and token arrive as files/import.json ({"relay_url": "wss://.../device", "device_token":
 * "..."}), pushed over adb (run-as) from the owner's environment variables: the app moves them into
 * its settings and deletes the file, so the token is never typed and never left in the clear.
 */
class Prefs(private val context: Context) {
    private val prefs = context.getSharedPreferences("relay", Context.MODE_PRIVATE)
    private val tokenFile get() = File(context.filesDir, "device_token.sealed")
    private val cipher = KeystoreCipher()

    var enabled: Boolean
        get() = prefs.getBoolean("enabled", false)
        set(value) = prefs.edit().putBoolean("enabled", value).apply()

    val relayUrl: String? get() = prefs.getString("relay_url", null)

    fun deviceToken(): String? = runCatching {
        if (!tokenFile.exists()) null else String(cipher.decrypt(tokenFile.readBytes()), Charsets.UTF_8)
    }.onFailure { Log.w(TAG, "could not read the device token", it) }.getOrNull()

    fun hasCredentials(): Boolean = relayUrl != null && tokenFile.exists()

    /** Takes files/import.json in, if it is there. True if something was imported. */
    fun importPending(): Boolean {
        val file = File(context.filesDir, "import.json")
        if (!file.exists()) return false
        return try {
            val obj = Json.parseToJsonElement(file.readText()) as JsonObject
            val url = (obj["relay_url"] as? JsonPrimitive)?.content
            val token = (obj["device_token"] as? JsonPrimitive)?.content
            if (url != null) {
                require(url.startsWith("wss://") || url.startsWith("ws://127.0.0.1") || url.startsWith("ws://localhost")) {
                    "relay_url must use wss:// (ws:// only to this phone itself, for testing)"
                }
                prefs.edit().putString("relay_url", url).apply()
            }
            if (token != null) {
                require(token.length >= 32) { "device_token is too short" }
                tokenFile.writeBytes(cipher.encrypt(token.toByteArray(Charsets.UTF_8)))
            }
            Log.i(TAG, "imported relay settings (url: ${url != null}, token: ${token != null})")
            true
        } catch (e: Exception) {
            Log.w(TAG, "could not import relay settings: ${e.message}")
            false
        } finally {
            file.delete()
        }
    }

    private companion object {
        const val TAG = "PhoneRelay"
    }
}
