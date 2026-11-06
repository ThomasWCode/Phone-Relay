package com.thomaswcode.phonerelay

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.view.Surface
import java.io.File

/**
 * A screen recording's H.264 encoder and MP4 writer, fed through a surface (the projection's
 * mirror), as screenrecord records. It repeats the last frame while the screen is still, so a
 * still screen's recording runs its whole length: MediaRecorder's ended at its last frame (found on
 * the phone: 0.01 s for 3 s of a still home screen, where screenrecord's ran the full time).
 */
class ScreenEncoder(file: File, width: Int, height: Int, bitRate: Int) {
    private val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
    private val muxer: MediaMuxer
    val surface: Surface
    private val drain = Thread(::drainLoop, "PhoneRelay-encoder")
    @Volatile private var samples = 0
    @Volatile private var failure: Throwable? = null

    init {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
            setInteger(MediaFormat.KEY_FRAME_RATE, 30)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, REPEAT_US)
        }
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            surface = codec.createInputSurface()
            muxer = MediaMuxer(file.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        } catch (e: Exception) {
            codec.release()
            throw e
        }
    }

    fun start() {
        codec.start()
        drain.start()
    }

    /** Moves each encoded frame into the MP4, until the end of the stream. */
    private fun drainLoop() {
        val info = MediaCodec.BufferInfo()
        var track = -1
        try {
            while (true) {
                val i = codec.dequeueOutputBuffer(info, 100_000)
                if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    track = muxer.addTrack(codec.outputFormat)
                    muxer.start()
                } else if (i >= 0) {
                    val buf = codec.getOutputBuffer(i)
                    if (buf != null && info.size > 0 && track >= 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        muxer.writeSampleData(track, buf, info)
                        samples++
                    }
                    codec.releaseOutputBuffer(i, false)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
            }
        } catch (e: Throwable) {
            failure = e
        }
    }

    /** Ends the video and writes the file out; true if it holds a frame and closed cleanly. */
    fun stop(): Boolean {
        runCatching { codec.signalEndOfInputStream() }
        if (drain.isAlive) drain.join(STOP_MS)
        val wrote = samples > 0
        runCatching { codec.stop() }
        runCatching { codec.release() }
        val closed = runCatching { muxer.stop() }.isSuccess // it throws if no frame ever came
        runCatching { muxer.release() }
        runCatching { surface.release() }
        return wrote && closed && failure == null
    }

    /** Frees it without finishing a video: a start that failed. */
    fun release() {
        runCatching { codec.release() }
        runCatching { muxer.release() }
        runCatching { surface.release() }
    }

    private companion object {
        /** A still screen's frame comes again after this long (µs). */
        const val REPEAT_US = 100_000L
        const val STOP_MS = 3_000L
    }
}
