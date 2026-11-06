package com.thomaswcode.phonerelay

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.WindowManager
import android.view.accessibility.AccessibilityNodeInfo
import com.thomaswcode.phonerelay.commands.CommandError
import com.thomaswcode.phonerelay.commands.Consent
import com.thomaswcode.phonerelay.commands.Size
import com.thomaswcode.phonerelay.commands.Video
import com.thomaswcode.phonerelay.commands.Wav
import java.io.File
import java.io.RandomAccessFile
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Screen recording and sound capture, as the adb client's screenrecord and helper do them, with
 * what an app has: MediaProjection and the microphone. Android asks for consent before each
 * projection (a System UI prompt, which the accessibility service answers: Consent), and lets one
 * run only in a foreground service of its type: RelayService takes the types on for the capture,
 * and its notification says what is being captured.
 */
class Capture(private val context: Context, private val scope: CoroutineScope) {
    /** The projection and RelayService's capture types, while a recording or a sound capture uses them. */
    private class Session(val projection: MediaProjection?, val types: Int) {
        var users = 1
        @Volatile var stopped = false
    }

    private class Recording(val session: Session, val recorder: MediaRecorder, val display: VirtualDisplay, val file: File) {
        var timer: Job? = null
        var done = false
        var ok = true
    }

    /** A capture waiting for Android's consent prompt to be answered. */
    private class Request(val types: Int, val label: String) {
        val result = CompletableDeferred<Session>()
    }

    private val projections = context.getSystemService(MediaProjectionManager::class.java)
    private var session: Session? = null
    private var recording: Recording? = null
    @Volatile private var request: Request? = null
    /** How many times end() has run: a recording that started across one is not kept. */
    private var ends = 0

    private fun micAllowed() = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun relay(): RelayService = RelayService.instance
        ?: throw CommandError("stopped", "the relay service is not running")

    /** A session with a projection (`projection`) or the microphone alone; one already running is shared. */
    private suspend fun acquire(projection: Boolean, label: String): Session {
        synchronized(this) {
            session?.let { s ->
                if (s.stopped || (projection && s.projection == null) || (!projection && s.types and ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE == 0)) {
                    throw CommandError("busy", "another capture is running: stop it first (/record/stop)")
                }
                s.users++
                return s
            }
        }
        // The microphone type too when it is allowed: then sound can be captured during a screen recording.
        val mic = if (micAllowed()) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        if (!projection) {
            try {
                relay().captureTypes(mic, label)
            } catch (e: Exception) {
                throw CommandError("permission_missing", "Android would not let the app use the microphone now: ${e.message}")
            }
            return Session(null, mic).also { synchronized(this) { session = it } }
        }
        return consent(ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or mic, label)
    }

    private fun release(s: Session) {
        synchronized(this) {
            if (--s.users > 0) return
            if (session === s) session = null
        }
        runCatching { s.projection?.stop() }
        runCatching { RelayService.instance?.captureTypes(0, null) }
    }

    // ---- consent ----

    /** Asks Android for a projection (ProjectionActivity), answers its prompt, and waits for the session. */
    private suspend fun consent(types: Int, label: String): Session {
        val a11y = PhoneAccessibilityService.instance
            ?: throw CommandError("permission_missing", "the Phone Relay accessibility service is off: it answers Android's capture prompt")
        val req = Request(types, label)
        request = req
        try {
            a11y.startActivity(
                Intent(context, ProjectionActivity::class.java).addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION or Intent.FLAG_ACTIVITY_NO_USER_ACTION,
                ),
            )
            if (withTimeoutOrNull(PROMPT_MS) { pressStart(a11y) } == null) {
                unanswered(a11y)
                throw CommandError("device_error", "Android's capture prompt did not appear, or the app could not answer it")
            }
            return withTimeoutOrNull(PROMPT_MS) { req.result.await() }
                ?: throw CommandError("device_error", "Android gave no capture after its prompt was answered")
        } finally {
            if (request === req) request = null
            ProjectionActivity.close() // and the prompt with it, if it is still open
        }
    }

    /** Presses the prompt's start button once it is on the screen. */
    private suspend fun pressStart(a11y: PhoneAccessibilityService) {
        val label = context.getString(R.string.app_name)
        while (true) {
            for (w in a11y.windows) {
                val root = w.root ?: continue
                val button = Consent.startButton(AndroidDevice.Node(root), label) as? AndroidDevice.Node ?: continue
                if (button.n.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return
            }
            delay(100)
        }
    }

    /** Logs the buttons of a prompt left unanswered, so a new wording can be added. */
    private fun unanswered(a11y: PhoneAccessibilityService) {
        val label = context.getString(R.string.app_name)
        for (w in a11y.windows) {
            val root = AndroidDevice.Node(w.root ?: continue)
            if (root.pkg != Consent.SYSTEM_UI || !mentions(root, label)) continue
            Log.w(TAG, "capture prompt not answered; its buttons: " + buttons(root).joinToString())
            return
        }
        Log.w(TAG, "capture prompt not on the screen")
    }

    private fun mentions(n: com.thomaswcode.phonerelay.commands.UiNode, label: String): Boolean =
        (n.text ?: "").contains(label) || n.children.any { mentions(it, label) }

    private fun buttons(n: com.thomaswcode.phonerelay.commands.UiNode): List<String> =
        (if (n.clickable) listOf("${n.viewId}:${n.text ?: n.desc}") else emptyList()) + n.children.flatMap(::buttons)

    /** ProjectionActivity's result: while it is in front, RelayService takes the capture types on and the projection starts. */
    fun consented(resultCode: Int, data: Intent?) {
        val req = request ?: return // given up on already
        if (resultCode != Activity.RESULT_OK || data == null) {
            req.result.completeExceptionally(CommandError("device_error", "Android's capture prompt was declined"))
            return
        }
        try {
            relay().captureTypes(req.types, req.label)
            val projection = projections.getMediaProjection(resultCode, data) ?: throw IllegalStateException("Android gave no projection")
            val s = Session(projection, req.types)
            // Android can stop a projection on its own (Stop in the status bar, among others): the capture ends there.
            projection.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() = stopped(s)
            }, Handler(Looper.getMainLooper()))
            synchronized(this) { session = s }
            if (!req.result.complete(s)) release(s)
        } catch (e: Exception) {
            runCatching { RelayService.instance?.captureTypes(0, null) }
            req.result.completeExceptionally(CommandError("device_error", "the capture could not start: ${e.message}"))
        }
    }

    private fun stopped(s: Session) {
        s.stopped = true
        // Off the main thread: a stop in progress elsewhere is waited for.
        recording?.takeIf { it.session === s }?.let { rec -> scope.launch(Dispatchers.IO) { finish(rec) } }
    }

    // ---- screen recording ----

    suspend fun startRecording(seconds: Double, size: Size?, bitRate: Int) {
        drop() // a recording left running or unfetched is dropped, as in the adb client
        val generation = synchronized(this) { ends }
        val s = acquire(projection = true, label = "recording the screen")
        var recorder: MediaRecorder? = null
        var display: VirtualDisplay? = null
        val rec = try {
            val frame = videoSize(size)
            val file = File(context.cacheDir, "record.mp4").apply { delete() }
            recorder = MediaRecorder(context).apply {
                setVideoSource(MediaRecorder.VideoSource.SURFACE)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setVideoEncoder(MediaRecorder.VideoEncoder.H264)
                setVideoSize(frame.width, frame.height)
                setVideoFrameRate(30)
                setVideoEncodingBitRate(bitRate)
                setOutputFile(file)
                prepare()
            }
            val mirror = s.projection!!.createVirtualDisplay(
                "Phone Relay recording", frame.width, frame.height, context.resources.displayMetrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, recorder.surface, null, null,
            ) ?: throw CommandError("device_error", "Android would not mirror the screen for the recording")
            display = mirror
            recorder.start()
            Recording(s, recorder, mirror, file)
        } catch (e: Exception) {
            runCatching { recorder?.release() }
            runCatching { display?.release() }
            release(s)
            throw if (e is CommandError || e is CancellationException) e else CommandError("device_error", "the recording could not start: ${e.message}")
        }
        rec.timer = scope.launch { delay(Math.round(seconds * 1000)); finish(rec) }
        // The connection may have closed while this started (end() found no recording then): checked
        // against end() under the same lock, so a recording is either seen by end() or not kept.
        val active = currentCoroutineContext().isActive
        val kept = synchronized(this) { (active && ends == generation).also { if (it) recording = rec } }
        if (!kept) {
            finish(rec)
            rec.file.delete()
            throw CommandError("stopped", "the connection closed while the recording started")
        }
    }

    /**
     * Ends a recording, keeping its file for /record/stop (or the next start's drop). A second caller
     * (the timer and /record/stop together) waits for the first: stop() takes a moment to write the
     * file, and until it returns neither the file nor the projection may be touched.
     */
    private fun finish(rec: Recording) {
        synchronized(rec) {
            if (rec.done) return
            rec.timer?.cancel()
            rec.ok = runCatching { rec.recorder.stop() }.isSuccess // it throws when no frame arrived
            runCatching { rec.recorder.release() }
            runCatching { rec.display.release() }
            release(rec.session)
            rec.done = true
        }
    }

    /** Stops the recording; its MP4's path, or null if there is none. */
    fun stopRecording(): String? {
        val rec = synchronized(this) { recording.also { recording = null } } ?: return null
        finish(rec)
        if (!rec.ok || rec.file.length() == 0L) {
            rec.file.delete()
            throw CommandError("device_error", "the recording holds no video: no frame reached the encoder")
        }
        return rec.file.path
    }

    private fun drop() {
        val rec = synchronized(this) { recording.also { recording = null } } ?: return
        finish(rec)
        rec.file.delete()
    }

    /** As screenrecord: the size asked for, else the screen's, smaller if the phone's H.264 encoder cannot take it. */
    private fun videoSize(wanted: Size?): Size {
        val screen = wanted ?: context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds.let { Size(it.width(), it.height()) }
        val caps = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
            .filter { it.isEncoder && it.supportedTypes.any { t -> t.equals(MediaFormat.MIMETYPE_VIDEO_AVC, ignoreCase = true) } }
            .sortedByDescending { it.isHardwareAccelerated } // MediaRecorder's choice
            .firstOrNull()?.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC)?.videoCapabilities
            ?: throw CommandError("unsupported", "the phone has no H.264 encoder")
        return Video.fit(screen, caps.widthAlignment, caps.heightAlignment, caps::isSizeSupported)
            ?: throw CommandError("invalid_args", "the phone's encoder takes no size like ${screen.width}x${screen.height}")
    }

    // ---- sound ----

    /** Records `seconds` of the phone's sound output (playback capture) or its microphone to a WAV file; its path. */
    @SuppressLint("MissingPermission") // checked first: Android needs RECORD_AUDIO for both sources
    suspend fun recordAudio(seconds: Double, source: String): String {
        if (!micAllowed()) {
            throw CommandError("permission_missing", "allow the microphone for Phone Relay (the app's own screen has the button): Android needs it for both sources")
        }
        val output = source == "output"
        val s = acquire(projection = output, label = if (output) "recording the phone's sound" else "recording from the microphone")
        val file = File(context.cacheDir, "audio.wav")
        try {
            val format = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(Wav.RATE)
                .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
                .build()
            val min = AudioRecord.getMinBufferSize(Wav.RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT)
            val builder = AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(maxOf(min, Wav.RATE * Wav.BYTES_PER_FRAME / 10) * 2)
            if (output) {
                // What apps play for media, games and unlabelled sound; apps that opt out of capture (and calls) stay out.
                builder.setAudioPlaybackCaptureConfig(
                    AudioPlaybackCaptureConfiguration.Builder(s.projection!!)
                        .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                        .addMatchingUsage(AudioAttributes.USAGE_GAME)
                        .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                        .build(),
                )
            } else {
                builder.setAudioSource(MediaRecorder.AudioSource.MIC)
            }
            val rec = builder.build()
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                rec.release()
                throw CommandError("device_error", "Android would not start the sound capture")
            }
            try {
                withContext(Dispatchers.IO) {
                    RandomAccessFile(file, "rw").use { out ->
                        out.setLength(0)
                        out.write(ByteArray(44)) // the header, filled in at the end
                        val total = Wav.dataBytes(seconds)
                        val buf = ByteArray(Wav.RATE * Wav.BYTES_PER_FRAME / 10)
                        var written = 0L
                        rec.startRecording()
                        while (written < total) {
                            ensureActive()
                            if (s.stopped) throw CommandError("device_error", "Android stopped the capture (Stop in the status bar, or a reason of its own)")
                            val n = rec.read(buf, 0, minOf(buf.size.toLong(), total - written).toInt())
                            if (n < 0) throw CommandError("device_error", "the sound capture failed (AudioRecord.read $n)")
                            out.write(buf, 0, n)
                            written += n
                        }
                        rec.stop()
                        out.seek(0)
                        out.write(Wav.header(written))
                    }
                }
            } finally {
                rec.release()
            }
            return file.path
        } catch (e: Throwable) {
            file.delete()
            throw e
        } finally {
            release(s)
        }
    }

    /** The owner's kill switches and a closed connection: a recording stops and is deleted, a prompt is given up. */
    fun end() {
        synchronized(this) { ends++ }
        drop()
        request?.result?.cancel()
        ProjectionActivity.close()
    }

    private companion object {
        const val TAG = "PhoneRelay"
        const val PROMPT_MS = 10_000L
    }
}
