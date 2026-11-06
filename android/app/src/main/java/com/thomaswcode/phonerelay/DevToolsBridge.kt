package com.thomaswcode.phonerelay

import com.thomaswcode.phonerelay.commands.CommandError
import com.thomaswcode.phonerelay.commands.DevToolsLink
import io.github.muntashirakon.adb.AdbStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.Socket
import java.net.SocketAddress
import java.util.concurrent.TimeUnit
import javax.net.SocketFactory
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlin.coroutines.resumeWithException
import okhttp3.Call
import okhttp3.Callback
import okhttp3.ConnectionPool
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/**
 * Chrome DevTools through the shell bridge: each HTTP request and WebSocket goes over its own adb
 * stream to the DevTools socket (localabstract:chrome_devtools_remote), as the adb client's
 * `adb forward` does, but with no port opened on the phone: OkHttp's sockets here are adb streams,
 * so no other app on the phone can reach DevTools through the app.
 */
class DevToolsBridge(private val adb: AdbShell, socket: String, private val version: String) : DevToolsLink {
    private val client = OkHttpClient.Builder()
        .socketFactory(StreamSocketFactory { runBlocking { adb.open("localabstract:$socket") } })
        .proxy(Proxy.NO_PROXY)
        .connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS)) // a stream is never reused
        .retryOnConnectionFailure(false)
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // the command's own deadline stops a slow call
        .build()

    /**
     * Cancelling the coroutine (the command's deadline, a closed connection) cancels the call, which
     * closes its stream and so ends a blocked connect, request or body read: a Chrome that accepts the
     * connection and never answers cannot hold the command queue.
     */
    override suspend fun http(path: String, method: String): JsonElement = suspendCancellableCoroutine { cont ->
        val request = Request.Builder()
            .url("http://127.0.0.1$path")
            .method(method, if (method == "PUT" || method == "POST") ByteArray(0).toRequestBody() else null)
            .header("User-Agent", "phone-relay-app/$version")
            .build()
        val call = client.newCall(request)
        cont.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (cont.isActive) cont.resumeWithException(devtoolsError(e))
            }

            override fun onResponse(call: Call, response: Response) {
                response.use {
                    val result = try {
                        val body = it.body?.string().orEmpty() // a cancelled call ends this read with an IOException
                        if (!it.isSuccessful) throw CommandError("devtools_failed", "$method $path: HTTP ${it.code} ${body.take(200)}")
                        Result.success(runCatching { Json.parseToJsonElement(body) }.getOrElse { JsonPrimitive(body) })
                    } catch (e: IOException) {
                        Result.failure(devtoolsError(e))
                    } catch (e: CommandError) {
                        Result.failure(e)
                    }
                    if (cont.isActive) cont.resumeWith(result)
                }
            }
        })
    }

    override suspend fun cdp(path: String, method: String, params: JsonObject): JsonObject {
        val reply = CompletableDeferred<JsonObject>()
        val call = buildJsonObject { put("id", 1); put("method", method); put("params", params) }.toString()
        val ws = client.newWebSocket(Request.Builder().url("ws://127.0.0.1$path").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(call)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                // Events may come first; the reply carries id 1.
                val msg = runCatching { Json.parseToJsonElement(text) as? JsonObject }.getOrNull() ?: return
                if ((msg["id"] as? JsonPrimitive)?.intOrNull == 1) reply.complete(msg)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                reply.completeExceptionally(devtoolsError(t))
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                reply.completeExceptionally(CommandError("devtools_failed", "the target closed before replying"))
            }
        })
        val msg = try {
            reply.await()
        } finally {
            ws.cancel()
        }
        (msg["error"] as? JsonObject)?.let { throw CommandError("devtools_failed", it.toString().take(300)) }
        return msg["result"] as? JsonObject ?: JsonObject(emptyMap())
    }

    /** Ends the bridge's connections (each stream closes with its socket). */
    fun close() {
        client.dispatcher.cancelAll()
        client.connectionPool.evictAll()
    }
}

/** The shell bridge's refusal, as the IOException OkHttp expects: anything else would be rethrown on its own threads. */
private class BridgeException(val error: CommandError) : IOException(error.message)

/** The command's error for a failed DevTools call. */
private fun devtoolsError(t: Throwable): CommandError = when {
    t is CommandError -> t
    t is BridgeException -> t.error
    t.cause is BridgeException -> (t.cause as BridgeException).error
    else -> CommandError("devtools_unavailable", "is Chrome running? ${t.javaClass.simpleName}: ${t.message}")
}

/** OkHttp's sockets, each a fresh adb stream: connect() has nothing to do. */
private class StreamSocketFactory(private val open: () -> AdbStream) : SocketFactory() {
    override fun createSocket(): Socket = StreamSocket(
        try {
            open()
        } catch (e: CommandError) {
            throw BridgeException(e)
        } catch (e: IOException) {
            throw e
        } catch (e: Exception) {
            throw IOException(e)
        },
    )
    override fun createSocket(host: String?, port: Int): Socket = createSocket()
    override fun createSocket(host: String?, port: Int, localHost: InetAddress?, localPort: Int): Socket = createSocket()
    override fun createSocket(host: InetAddress?, port: Int): Socket = createSocket()
    override fun createSocket(address: InetAddress?, port: Int, localAddress: InetAddress?, localPort: Int): Socket = createSocket()
}

/** A socket over an adb stream, as far as OkHttp uses one. */
private class StreamSocket(private val stream: AdbStream) : Socket() {
    private val input: InputStream = AdbShell.EndAwareInput(stream)
    private val output: OutputStream = stream.openOutputStream()
    private var timeout = 0
    private val loopback = InetAddress.getLoopbackAddress()

    override fun connect(endpoint: SocketAddress?) = Unit
    override fun connect(endpoint: SocketAddress?, timeout: Int) = Unit
    override fun getInputStream(): InputStream = input
    override fun getOutputStream(): OutputStream = output
    override fun isConnected() = true
    override fun isBound() = true
    override fun isClosed() = stream.isClosed
    override fun isInputShutdown() = false
    override fun isOutputShutdown() = false
    override fun shutdownInput() = Unit
    override fun shutdownOutput() = Unit
    override fun setSoTimeout(timeout: Int) { this.timeout = timeout }
    override fun getSoTimeout() = timeout
    override fun setTcpNoDelay(on: Boolean) = Unit
    override fun setKeepAlive(on: Boolean) = Unit
    override fun getInetAddress(): InetAddress = loopback
    override fun getLocalAddress(): InetAddress = loopback
    override fun getPort() = 80
    override fun getLocalPort() = 0
    override fun getRemoteSocketAddress(): SocketAddress = InetSocketAddress(loopback, 80)
    override fun getLocalSocketAddress(): SocketAddress = InetSocketAddress(loopback, 0)

    override fun close() {
        runCatching { stream.close() }
    }
}
