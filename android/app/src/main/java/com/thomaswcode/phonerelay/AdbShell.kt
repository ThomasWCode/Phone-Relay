package com.thomaswcode.phonerelay

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import android.util.Log
import com.thomaswcode.phonerelay.commands.CommandError
import io.github.muntashirakon.adb.AbsAdbConnectionManager
import io.github.muntashirakon.adb.AdbPairingRequiredException
import io.github.muntashirakon.adb.AdbStream
import io.github.muntashirakon.adb.android.AdbMdns
import java.io.File
import java.math.BigInteger
import java.net.InetSocketAddress
import java.net.Socket
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.cert.Certificate
import java.security.cert.CertificateFactory
import java.security.spec.PKCS8EncodedKeySpec
import java.util.Date
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.runInterruptible
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder

/**
 * The shell bridge: the app's own adb client to this phone's wireless debugging (libadb-android),
 * for what only the shell user can do: Chrome DevTools, any key anywhere, typing with no text
 * field. The owner pairs it once, with the code Settings shows, typed into the app's notification
 * (Pairing). Its key is its own (RSA, with a self-signed certificate, as adb's), sealed with the
 * Keystore cipher like the device token.
 *
 * It needs wireless debugging on: Android 17 keeps that to networks the owner allowed, and the
 * app turns it back on (when it holds WRITE_SECURE_SETTINGS, granted once over adb) when Android
 * has turned it off, for instance after a reboot.
 *
 * Off Wi-Fi (the owner's switch, `mobile`): wireless debugging is Wi-Fi only, so while the relay is
 * on the bridge has adbd listen on its classic TCP port as well (the `tcpip:` service, what
 * `adb tcpip` asks for), which answers on this phone's loopback on any network, and connects there.
 * The port can only be opened over wireless debugging, so on Wi-Fi, once after a reboot; it asks for
 * the same key. adbd listens on every interface (the shell user may not narrow it to loopback), so
 * the port is one drawn at random for this install, not adb's usual 5555, and it is closed again
 * (the `usb:` service) when the relay is turned off. adbd restarts to open it and to close it, which
 * ends every adb connection to the phone, a computer's too.
 */
class AdbShell(private val context: Context, private val relayOn: () -> Boolean) {
    private val keyFile = File(context.filesDir, "adb_key.sealed")
    private val certFile = File(context.filesDir, "adb_cert.sealed")
    private val prefs = context.getSharedPreferences("shell", Context.MODE_PRIVATE)
    private val cipher = KeystoreCipher("phone-relay-adb")
    private val lock = Mutex()
    private var manager: Manager? = null
    /** The classic port did not take the app's key: not asked for again (each asking restarts adbd). */
    @Volatile private var tcpFailed = false
    private val _mobilePort = MutableStateFlow<Boolean?>(null)
    /** Whether the port for mobile data is open, as last seen (null: not looked at yet). */
    val mobilePort: StateFlow<Boolean?> get() = _mobilePort

    private class Manager(private val key: PrivateKey, private val cert: Certificate) : AbsAdbConnectionManager() {
        init {
            setApi(Build.VERSION.SDK_INT)
            setTimeout(10, TimeUnit.SECONDS)
            // On the classic port, a key adbd does not know is refused, never offered: offering it
            // would put Android's "Allow USB debugging?" prompt on the screen.
            setThrowOnUnauthorised(true)
        }

        override fun getPrivateKey() = key
        override fun getCertificate() = cert
        override fun getDeviceName() = "Phone Relay"
    }

    /** Whether the owner has paired the app with wireless debugging. */
    val paired: Boolean get() = prefs.getBoolean("paired", false)

    /** Whether the app may switch wireless debugging back on itself. */
    fun canEnable(): Boolean =
        context.checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS") == PackageManager.PERMISSION_GRANTED

    private fun wirelessDebuggingOn(): Boolean = Settings.Global.getInt(context.contentResolver, ADB_WIFI_ENABLED, 0) == 1

    /** Whether the owner wants the bridge off Wi-Fi too (the app's own screen, Shell access). */
    val mobile: Boolean get() = prefs.getBoolean("mobile", false)

    /** The classic port: drawn once for this install and kept, below the range adbd and Android pick their own from. */
    private fun tcpPort(): Int {
        val kept = prefs.getInt("tcp", 0)
        if (kept > 0) return kept
        return (TCP_PORT_FIRST + SecureRandom().nextInt(TCP_PORT_COUNT)).also { prefs.edit().putInt("tcp", it).apply() }
    }

    /** Whether the classic port answers on this phone: a bare TCP connection, which shows adbd no key. */
    private fun tcpListening(): Boolean {
        val port = prefs.getInt("tcp", 0)
        if (port <= 0) return false
        return runCatching { Socket().use { it.connect(InetSocketAddress(LOCALHOST, port), 500) } }.isSuccess
    }

    /** The key and certificate, made on first use and kept sealed. */
    private fun manager(): Manager {
        manager?.let { return it }
        val made = if (keyFile.exists() && certFile.exists()) {
            val key = KeyFactory.getInstance("RSA").generatePrivate(PKCS8EncodedKeySpec(cipher.decrypt(keyFile.readBytes())))
            val cert = CertificateFactory.getInstance("X.509").generateCertificate(cipher.decrypt(certFile.readBytes()).inputStream())
            Manager(key, cert)
        } else {
            val pair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048, SecureRandom()) }.generateKeyPair()
            val name = X500Name("CN=Phone Relay")
            val now = System.currentTimeMillis()
            val holder = JcaX509v3CertificateBuilder(
                name, BigInteger(64, SecureRandom()), Date(now - 86_400_000), Date(now + 30L * 365 * 86_400_000), name, pair.public,
            ).build(JcaContentSignerBuilder("SHA256withRSA").build(pair.private))
            val cert = JcaX509CertificateConverter().getCertificate(holder)
            keyFile.writeBytes(cipher.encrypt(pair.private.encoded))
            certFile.writeBytes(cipher.encrypt(cert.encoded))
            Manager(pair.private, cert)
        }
        manager = made
        return made
    }

    /** The port an adbd service of `type` announces over mDNS on this phone, or -1. */
    private fun discover(type: String, timeoutMs: Long): Int {
        val port = AtomicInteger(-1)
        val found = CountDownLatch(1)
        val mdns = AdbMdns(context, type) { _, p ->
            if (p > 0) port.set(p)
            found.countDown()
        }
        mdns.start()
        try {
            found.await(timeoutMs, TimeUnit.MILLISECONDS)
        } finally {
            mdns.stop()
        }
        return port.get()
    }

    /** Connected to this phone's adbd, connecting (and switching wireless debugging on) first if needed. */
    private suspend fun connected(): Manager = lock.withLock {
        if (!paired) throw CommandError("shell_unavailable", "pair the app with wireless debugging first: the app's own screen, Shell access")
        val m = withContext(Dispatchers.IO) { manager() }
        if (m.isConnected) return m
        if (mobile && connectTcp(m)) return m
        if (!wirelessDebuggingOn()) {
            if (!canEnable()) throw CommandError("shell_unavailable", "wireless debugging is off (Settings > Developer options), and the app may not switch it on")
            Settings.Global.putInt(context.contentResolver, ADB_WIFI_ENABLED, 1)
            Log.i(TAG, "switched wireless debugging on for the shell bridge")
            delay(1500) // adbd takes a moment to listen and announce itself
        }
        connect(m)
        if (mobile && relayOn() && !tcpFailed) openTcp(m)
        m
    }

    /** Connects to adbd's classic port, if it is open: the route that works off Wi-Fi. */
    private suspend fun connectTcp(m: Manager): Boolean = withContext(Dispatchers.IO) {
        val port = prefs.getInt("tcp", 0)
        (port > 0 && runCatching { m.connect(LOCALHOST, port) }.getOrDefault(false)).also { if (it) _mobilePort.value = true }
    }

    /**
     * Asks adbd for a service it restarts for ("tcpip:<port>", "usb:"): it answers, then restarts,
     * which ends the connection the request went over.
     */
    private suspend fun restartAdbd(m: Manager, service: String) {
        withTimeoutOrNull(RESTART_MS) {
            runInterruptible(Dispatchers.IO) {
                runCatching {
                    val stream = m.openStream(service)
                    try {
                        readAll(stream)
                    } finally {
                        runCatching { stream.close() }
                    }
                }
            }
        }
        withContext(Dispatchers.IO) { runCatching { m.disconnect() } }
    }

    /**
     * Has adbd listen on the classic port too, and moves the connection onto it. If the port then
     * does not take the app's key, it is closed again and the bridge stays on wireless debugging.
     */
    private suspend fun openTcp(m: Manager) {
        restartAdbd(m, "tcpip:${tcpPort()}")
        repeat(TCP_TRIES) {
            delay(TCP_WAIT_MS) // adbd is back within a second or two
            if (connectTcp(m)) {
                Log.i(TAG, "opened adbd's port for mobile data")
                return
            }
        }
        tcpFailed = true
        Log.w(TAG, "adbd's port for mobile data did not take the app's key: closing it, staying on wireless debugging")
        connect(m)
        restartAdbd(m, "usb:")
        delay(TCP_TRIES * TCP_WAIT_MS)
        _mobilePort.value = withContext(Dispatchers.IO) { tcpListening() }
        connect(m)
    }

    /**
     * Opens the port for mobile data ahead of need (Wi-Fi just joined, the owner's switch just set):
     * it can only be opened over wireless debugging, so once the phone has left Wi-Fi it is too late.
     */
    suspend fun prepare() {
        if (!mobile || !paired || !relayOn()) return
        try {
            connected()
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.i(TAG, "the port for mobile data is not open yet: ${e.message}")
        }
    }

    /**
     * Closes the port for mobile data: the relay was turned off, or the owner no longer wants the
     * bridge off Wi-Fi. adbd goes back to USB and wireless debugging alone. False if the port still
     * answers afterwards (adbd could not be reached with the app's key to be told).
     */
    suspend fun closeTcp(): Boolean = lock.withLock {
        if (mobile && relayOn()) return true // on again meanwhile
        val open = withContext(Dispatchers.IO) { tcpListening() }
        if (open) {
            val m = withContext(Dispatchers.IO) { manager() }
            if (!m.isConnected && !connectTcp(m)) runCatching { connect(m) }
            if (m.isConnected) {
                restartAdbd(m, "usb:")
                repeat(TCP_TRIES) {
                    delay(TCP_WAIT_MS)
                    if (!withContext(Dispatchers.IO) { tcpListening() }) {
                        Log.i(TAG, "closed adbd's port for mobile data")
                        _mobilePort.value = false
                        return true
                    }
                }
            }
            Log.w(TAG, "adbd's port for mobile data is still open: it could not be closed")
        }
        _mobilePort.value = open
        !open
    }

    /** The owner's switch: on, the port is opened now if the phone is on Wi-Fi; off, it is closed. */
    suspend fun setMobile(on: Boolean) {
        prefs.edit().putBoolean("mobile", on).apply()
        if (!on) {
            closeTcp()
            return
        }
        tcpFailed = false
        // A connection over wireless debugging would be kept as it is: a fresh one opens the port.
        lock.withLock { withContext(Dispatchers.IO) { runCatching { manager?.disconnect() } } }
        prepare()
    }

    /** Connects to this phone's adbd: the port it used last, else the one it announces over mDNS. */
    private suspend fun connect(m: Manager) = withContext(Dispatchers.IO) {
        val last = prefs.getInt("port", -1)
        val ok = (last > 0 && runCatching { m.connect(LOCALHOST, last) }.getOrDefault(false)) || run {
            val port = discover(AdbMdns.SERVICE_TYPE_TLS_CONNECT, DISCOVER_MS)
            if (port <= 0) {
                throw CommandError(
                    "shell_unavailable",
                    "wireless debugging is not answering on this phone (not on Wi-Fi, or a network not allowed for it)" +
                        if (mobile) "; its port for mobile data is opened on Wi-Fi, once after a reboot or after the relay is turned on" else "",
                )
            }
            prefs.edit().putInt("port", port).apply()
            try {
                m.connect(LOCALHOST, port)
            } catch (e: AdbPairingRequiredException) {
                prefs.edit().putBoolean("paired", false).apply()
                throw CommandError("shell_unavailable", "wireless debugging no longer knows the app: pair it again (the app's own screen)")
            }
        }
        if (!ok) throw CommandError("shell_unavailable", "wireless debugging refused the app's key")
    }

    /**
     * Opens an adb service on this phone: "exec:<command>", "localabstract:<socket>", ... `guard`
     * runs once the connection is up, immediately before the service is requested (and again before
     * a second try): it throws to refuse, and what it throws is not retried.
     */
    suspend fun open(destination: String, guard: (() -> Unit)? = null): AdbStream {
        val m = connected()
        guard?.invoke()
        return try {
            runInterruptible(Dispatchers.IO) { m.openStream(destination) }
        } catch (e: CommandError) {
            throw e
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            // A connection gone stale (wireless debugging restarted): once more on a fresh one.
            lock.withLock { runCatching { m.disconnect() } }
            val fresh = connected()
            guard?.invoke()
            runInterruptible(Dispatchers.IO) { fresh.openStream(destination) }
        }
    }

    /**
     * Runs a command as the shell user; its output (no exit code). Through adb's exec service (what
     * `adb exec-out` uses): raw pipes. The shell service runs a command on a pty, which cut /proc/net/unix
     * (55 KB) off after 8 KB on the phone and hung the first call for 30 s.
     */
    suspend fun run(command: String, guard: (() -> Unit)? = null): String {
        val stream = open("exec:$command", guard)
        return try {
            runInterruptible(Dispatchers.IO) { readAll(stream) }.also {
                // Sizes only: what is typed through here must stay out of the log.
                Log.i(TAG, "shell: a ${command.length}-character command gave ${it.size} bytes")
            }.toString(Charsets.UTF_8)
        } finally {
            runCatching { stream.close() }
        }
    }

    /** Runs a command as run() does, writing its output byte for byte to `file` (a file's bytes, which text would not keep). */
    suspend fun runToFile(command: String, file: File, guard: (() -> Unit)? = null) {
        val stream = open("exec:$command", guard)
        try {
            runInterruptible(Dispatchers.IO) {
                file.outputStream().use { out -> EndAwareInput(stream).copyTo(out) }
            }
            Log.i(TAG, "shell: a ${command.length}-character command gave ${file.length()} bytes to a file")
        } finally {
            runCatching { stream.close() }
        }
    }

    /**
     * Runs a command with `input` as its standard input, as `adb exec-in` does: the bytes are sent,
     * then the stream is closed, which adbd takes as the input's end once it has written them.
     */
    suspend fun runWithInput(command: String, input: File, guard: (() -> Unit)? = null) {
        val stream = open("exec:$command", guard)
        try {
            runInterruptible(Dispatchers.IO) {
                // A bounded buffer: the input is never in memory whole
                input.inputStream().use { source -> stream.openOutputStream().let { source.copyTo(it); it.flush() } }
            }
            Log.i(TAG, "shell: a ${command.length}-character command was given ${input.length()} bytes")
        } finally {
            runCatching { stream.close() }
        }
    }

    /** Everything the stream gives until the command ends. */
    private fun readAll(stream: AdbStream): ByteArray {
        val input = EndAwareInput(stream)
        val out = java.io.ByteArrayOutputStream()
        val buf = ByteArray(8192)
        while (true) {
            val n = input.read(buf, 0, buf.size)
            if (n < 0) return out.toByteArray()
            out.write(buf, 0, n)
        }
    }

    /**
     * Pairs with the code Settings > Developer options > Wireless debugging > Pair device with
     * pairing code shows, at the pairing port it announces over mDNS.
     */
    suspend fun pair(code: String) = withContext(Dispatchers.IO) {
        val port = discover(AdbMdns.SERVICE_TYPE_TLS_PAIRING, DISCOVER_MS)
        if (port <= 0) throw CommandError("shell_unavailable", "no pairing code is showing: open Pair device with pairing code first")
        val ok = try {
            manager().pair(LOCALHOST, port, code)
        } catch (e: Exception) {
            throw CommandError("shell_unavailable", "pairing failed: ${e.message ?: e.javaClass.simpleName}")
        }
        if (!ok) throw CommandError("shell_unavailable", "pairing failed: a wrong code?")
        prefs.edit().putBoolean("paired", true).remove("port").apply()
        lock.withLock { runCatching { manager?.disconnect() } }
    }

    /** Whether a pairing code is showing in Settings right now (its service is announced). */
    suspend fun pairingShowing(): Boolean = withContext(Dispatchers.IO) { discover(AdbMdns.SERVICE_TYPE_TLS_PAIRING, 1500) > 0 }

    /**
     * An InputStream over an adb stream, working around three libadb 3.1.1 faults found on the phone:
     * - AdbInputStream.read returns -1 as soon as the peer has closed the stream, dropping what a large
     *   payload still holds in its buffer (a 55 KB output came out as its first 8 KB), so this calls
     *   AdbStream.read, which looks at the buffer first;
     * - AdbStream.read ends a closed stream by throwing IOException("Stream closed.") where an
     *   InputStream returns -1, which this gives;
     * - when the peer's close arrives while payloads are still queued, AdbStream sets mPendingClose and
     *   its read, once the queue is drained, waits for a wake-up that never comes (half of the calls that
     *   printed more than a few bytes hung for the command's whole deadline: the close beat the reader).
     *   Nothing more can arrive after a close, so with it pending and nothing left to read, this ends
     *   the stream. mPendingClose is private: it is read by reflection, and if that is ever not
     *   possible the hang is back, not an error.
     */
    class EndAwareInput(private val stream: AdbStream) : java.io.InputStream() {
        override fun read(): Int {
            val one = ByteArray(1)
            return if (read(one, 0, 1) < 0) -1 else one[0].toInt() and 0xFF
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int = try {
            if (pendingClose?.getBoolean(stream) == true && stream.available() == 0) -1
            else stream.read(b, off, len)
        } catch (e: java.io.IOException) {
            if (stream.isClosed && e.message == "Stream closed.") -1 else throw e
        }

        override fun available(): Int = stream.available()
        override fun close() = stream.close()
    }

    private companion object {
        /** AdbStream.mPendingClose: the peer has closed the stream, and payloads are still queued. */
        val pendingClose: java.lang.reflect.Field? = runCatching {
            AdbStream::class.java.getDeclaredField("mPendingClose").apply { isAccessible = true }
        }.getOrNull()

        const val TAG = "PhoneRelay"
        const val ADB_WIFI_ENABLED = "adb_wifi_enabled"
        const val LOCALHOST = "127.0.0.1"
        const val DISCOVER_MS = 6_000L
        const val TCP_PORT_FIRST = 20_000
        const val TCP_PORT_COUNT = 12_000
        const val TCP_TRIES = 16
        const val TCP_WAIT_MS = 250L
        const val RESTART_MS = 3_000L
    }
}
