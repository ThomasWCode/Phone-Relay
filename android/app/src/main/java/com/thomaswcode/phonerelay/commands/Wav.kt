package com.thomaswcode.phonerelay.commands

/** /audio's file, as the adb client's helper writes it: 48 kHz, stereo, 16-bit PCM in a WAV file. */
object Wav {
    const val RATE = 48000
    const val BYTES_PER_FRAME = 4 // two channels of two bytes

    /** The audio data's length for `seconds`, in whole frames. */
    fun dataBytes(seconds: Double): Long = (seconds * RATE).toLong() * BYTES_PER_FRAME

    /** The 44-byte header in front of `dataBytes` of audio. */
    fun header(dataBytes: Long): ByteArray {
        val h = java.io.ByteArrayOutputStream(44)
        fun le(v: Long, n: Int) = repeat(n) { h.write(((v shr (8 * it)) and 0xFF).toInt()) }
        h.write("RIFF".toByteArray())
        le(36 + dataBytes, 4)
        h.write("WAVE".toByteArray())
        h.write("fmt ".toByteArray())
        le(16, 4)
        le(1, 2) // PCM
        le(2, 2) // stereo
        le(RATE.toLong(), 4)
        le(RATE.toLong() * BYTES_PER_FRAME, 4) // bytes per second
        le(BYTES_PER_FRAME.toLong(), 2) // block align
        le(16, 2) // bits per sample
        h.write("data".toByteArray())
        le(dataBytes, 4)
        return h.toByteArray()
    }
}
