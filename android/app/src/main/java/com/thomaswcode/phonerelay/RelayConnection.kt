package com.thomaswcode.phonerelay

import android.util.Log
import com.thomaswcode.phonerelay.commands.CommandError
import com.thomaswcode.phonerelay.commands.Commands
import com.thomaswcode.phonerelay.commands.Stream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import kotlin.random.Random
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/** Where the connection stands, for the app's screen, its notification, the tile and the widget. */
sealed class Status(val label: String) {
    data object Off : Status("Off")
    data object NoSettings : Status("No relay URL or device token yet")
    data object Connecting : Status("Connecting")
    data class Connected(val since: Long) : Status("Connected")
    data class Waiting(val why: String, val retryAt: Long) : Status("Reconnecting: $why")
}

/**
 * The phone's side of the relay, as the adb client's relay_loop and serve: dials out to the Worker
 * over WSS with the device token, says hello, and runs its commands one at a time, in order, while
 * still reading the socket, so each command's side messages (file chunks, acks) reach its Stream.
 * When the socket closes (the owner's disable, the toggle, a network drop) the running command is
 * cancelled. It reconnects with backoff, at once when the screen comes on (nudge).
 */
class RelayConnection(
    private val prefs: Prefs,
    private val commands: Commands,
    private val version: String,
    private val scope: CoroutineScope,
) {
    private val _status = MutableStateFlow<Status>(Status.Off)
    val status: StateFlow<Status> = _status

    private val client = OkHttpClient.Builder()
        // WebSocket pings: Cloudflare closes an idle socket, and a dead network shows within a minute.
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private val nudges = Channel<Unit>(Channel.CONFLATED)
    private var job: Job? = null

    @Synchronized
    fun start() {
        if (job?.isActive == true) return
        job = scope.launch(Dispatchers.IO) { loop() }
    }

    @Synchronized
    fun stop() {
        job?.cancel()
        job = null
        _status.value = Status.Off
    }

    /** Skip the backoff wait: the screen came on, so the network probably did too. */
    fun nudge() {
        nudges.trySend(Unit)
    }

    private suspend fun loop() {
        var backoff = 1.0
        while (scope.isActive) {
            prefs.importPending()
            val url = prefs.relayUrl
            val token = prefs.deviceToken()
            if (url == null || token == null) {
                _status.value = Status.NoSettings
                withTimeoutOrNull(30_000) { nudges.receive() }
                continue
            }
            _status.value = Status.Connecting
            val outcome = session(url, token)
            if (outcome.connected) backoff = 1.0
            val wait = when (outcome.httpStatus) {
                401 -> 300.0 // a wrong device token: retrying soon would not help
                403 -> 60.0 // the owner switched the relay off (/admin/disable)
                else -> backoff * (0.5 + Random.nextDouble())
            }
            val why = when (outcome.httpStatus) {
                401 -> "the relay rejected the device token"
                403 -> "the relay is disabled by its owner"
                else -> outcome.reason
            }
            _status.value = Status.Waiting(why, System.currentTimeMillis() + (wait * 1000).toLong())
            Log.i(TAG, "relay: $why; again in ${"%.0f".format(wait)} s")
            withTimeoutOrNull((wait * 1000).toLong()) { nudges.receive() }
            backoff = minOf(backoff * 2, 60.0)
        }
    }

    private class Outcome(val connected: Boolean, val httpStatus: Int?, val reason: String)

    private suspend fun session(url: String, token: String): Outcome {
        val queue = Channel<Pair<JsonObject, Stream?>>(Channel.UNLIMITED)
        val streams = ConcurrentHashMap<String, Stream>()
        val done = CompletableDeferred<Outcome>()
        var opened = false
        lateinit var socket: WebSocket

        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                opened = true
                _status.value = Status.Connected(System.currentTimeMillis())
                webSocket.send(
                    buildJsonObject {
                        put("type", "hello")
                        put("client", "android-app")
                        put("version", version)
                        put("capabilities", JsonArray(commands.capabilities.map { JsonPrimitive(it) }))
                    }.toString(),
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = runCatching { Json.parseToJsonElement(text) as? JsonObject }.getOrNull() ?: return
                val id = (msg["id"] as? JsonPrimitive)?.content
                val type = (msg["type"] as? JsonPrimitive)?.content
                if (type in Stream.SIDE_TYPES) {
                    if (id != null) streams[id]?.inbox?.trySend(msg) // a finished command's are dropped
                    return
                }
                if (id == null) return
                // Registered now, before the command runs, so chunks sent right behind it are kept.
                val stream = Stream(id) { out -> if (!webSocket.send(out.toString())) throw CommandError("cancelled", "the connection closed") }
                streams[id] = stream
                queue.trySend(msg to stream)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
                done.complete(Outcome(opened, null, "closed ($code${if (reason.isNotEmpty()) " $reason" else ""})"))
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                done.complete(Outcome(opened, null, "closed ($code)"))
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                done.complete(Outcome(opened, response?.code, response?.let { "HTTP ${it.code}" } ?: (t.message ?: t.javaClass.simpleName)))
            }
        }

        val request = Request.Builder()
            .url(url.replaceFirst("wss://", "https://").replaceFirst("ws://", "http://"))
            .header("Authorization", "Bearer $token")
            .header("User-Agent", "phone-relay-app/$version")
            .build()
        socket = client.newWebSocket(request, listener)

        val worker = scope.launch(Dispatchers.IO) {
            for ((msg, stream) in queue) { // one command at a time: actions must not interleave
                val reply = run(msg, stream)
                stream?.let { streams.remove(it.id) }
                socket.send(reply.toString())
            }
        }
        return try {
            done.await()
        } finally {
            // The connection is gone (or the toggle went off): stop the command it was running.
            worker.cancel()
            queue.close()
            socket.cancel()
            runCatching { commands.closed() }.onFailure { Log.w(TAG, "could not end a capture", it) }
        }
    }

    private suspend fun run(msg: JsonObject, stream: Stream?): JsonObject {
        val id = (msg["id"] as JsonPrimitive).content
        val cmd = (msg["cmd"] as? JsonPrimitive)?.content ?: ""
        val args = msg["args"] as? JsonObject ?: JsonObject(emptyMap())
        // Never the arguments: typed text, a page's script and file contents stay out of the log.
        Log.i(TAG, "command $cmd")
        return try {
            buildJsonObject { put("id", id); put("ok", true); put("result", commands.handle(cmd, args, stream)) }
        } catch (e: CommandError) {
            Log.i(TAG, "refused/failed $cmd: ${e.code}")
            buildJsonObject { put("id", id); put("ok", false); put("error", e.code); put("detail", e.detail.take(300)) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) { // never let one bad command take the connection down
            Log.w(TAG, "unexpected error in $cmd", e)
            buildJsonObject { put("id", id); put("ok", false); put("error", "device_error"); put("detail", e.javaClass.simpleName) }
        }
    }

    private companion object {
        const val TAG = "PhoneRelay"
    }
}
