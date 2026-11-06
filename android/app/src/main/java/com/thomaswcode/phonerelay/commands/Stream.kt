package com.thomaswcode.phonerelay.commands

import java.io.InputStream
import java.io.OutputStream
import java.util.Base64
import kotlinx.coroutines.channels.Channel
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put

/**
 * One command's side messages on the relay socket, as the adb client's Channel: a download (pull)
 * sends a start message, then chunks, never more than WINDOW ahead of the relay's acks; an upload
 * (push) receives chunks, acks each and ends at the relay's end message. The relay sends cancel if
 * the agent goes away.
 */
class Stream(val id: String, private val send: suspend (JsonObject) -> Unit) {
    val inbox = Channel<JsonObject>(Channel.UNLIMITED)

    private fun type(msg: JsonObject) = (msg["type"] as? JsonPrimitive)?.content

    private fun checked(msg: JsonObject): JsonObject {
        if (type(msg) == "cancel") throw CommandError("cancelled", "the relay cancelled the transfer")
        return msg
    }

    private suspend fun out(build: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit) =
        send(buildJsonObject { put("id", id); build() })

    suspend fun sendFile(input: InputStream, size: Long, mime: String, headers: Map<String, String>): Long {
        out {
            put("type", "start"); put("mime", mime); put("size", size)
            put("headers", buildJsonObject { headers.forEach { (k, v) -> put(k, v) } })
        }
        var acked = -1
        var seq = 0
        var total = 0L
        val buf = ByteArray(CHUNK)
        while (true) {
            val n = readFully(input, buf)
            if (n <= 0) break
            // Read what has arrived (acks, a cancel); wait only when the window is full.
            while (true) {
                val msg = inbox.tryReceive().getOrNull()?.let(::checked)
                    ?: if (seq - acked > WINDOW) checked(inbox.receive()) else break
                if (type(msg) == "ack") (msg["seq"] as? JsonPrimitive)?.intOrNull?.let { acked = maxOf(acked, it) }
            }
            val data = Base64.getEncoder().encodeToString(if (n == buf.size) buf else buf.copyOf(n))
            out { put("type", "chunk"); put("seq", seq); put("data", data) }
            seq++
            total += n
        }
        return total
    }

    suspend fun recvFile(output: OutputStream): Long {
        var total = 0L
        while (true) {
            val msg = checked(inbox.receive())
            when (type(msg)) {
                "end" -> return total
                "chunk" -> {
                    val data = Base64.getDecoder().decode((msg["data"] as? JsonPrimitive)?.content.orEmpty())
                    output.write(data)
                    total += data.size
                    out { put("type", "ack"); put("seq", (msg["seq"] as? JsonPrimitive)?.intOrNull ?: -1) }
                }
            }
        }
    }

    private fun readFully(input: InputStream, buf: ByteArray): Int {
        var n = 0
        while (n < buf.size) {
            val r = input.read(buf, n, buf.size - n)
            if (r < 0) break
            n += r
        }
        return n
    }

    companion object {
        const val CHUNK = 256 * 1024
        const val WINDOW = 8
        /** Side messages of a running command, routed to its Stream rather than queued as commands. */
        val SIDE_TYPES = setOf("chunk", "end", "ack", "cancel")
    }
}
