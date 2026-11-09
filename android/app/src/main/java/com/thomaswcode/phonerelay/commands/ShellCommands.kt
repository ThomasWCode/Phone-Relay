package com.thomaswcode.phonerelay.commands

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.intOrNull

/**
 * The shell commands of the release checks and the shell-user intent, and readers of what they
 * print: the same command lines and parsing as the adb client (phone_client.py's logcat_command,
 * parse_accessibility, parse_install, parse_settings, _list_command, parse_stat, cmd_intent), for
 * the app's shell bridge.
 */
object ShellCommands {
    /** Between two commands' output in one shell call. */
    const val SPLIT_MARKER = "PHONE_RELAY_SPLIT"
    /** Printed by a shell command when the path it was given does not exist or cannot be read. */
    const val MISSING_MARKER = "PHONE_RELAY_MISSING"
    /** Printed after an archive when tar did not finish cleanly (a file it could not read was left out). */
    const val TAR_FAILED_MARKER = "PHONE_RELAY_TAR_FAILED"
    const val DEFAULT_LOG_LINES = 500
    val LOG_LEVELS = listOf("V", "D", "I", "W", "E", "F")
    val LOG_BUFFERS = listOf("main", "system", "crash", "events", "radio", "kernel", "default", "all")
    val SETTINGS_NAMESPACES = listOf("system", "secure", "global")
    private val LOG_TAG_RE = Regex("^[\\w.$-]{1,64}$")
    private val LOG_TIME_RE = Regex("^(\\d{4}-)?\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}(\\.\\d{1,9})?$")
    // No part may start with "-", so none is read as an option of cat, rm or tar.
    private val DATA_PATH_RE = Regex("^[\\w.@+ ][\\w.@+ -]*(/[\\w.@+ ][\\w.@+ -]*)*/?$")
    private val CONTENT_URI_RE = Regex("^content://[\\w.-]+(/[\\w.%:@+-]*)*$")
    private val COLUMN_RE = Regex("^\\w{1,64}$")
    private val SORT_RE = Regex("^[\\w ,]{1,200}$")
    val SIGNALS = listOf("STOP", "CONT", "KILL")
    val NIGHT_MODES = listOf("yes", "no", "auto")
    /** Never force-stopped: the system's own, and what runs this app or the adb client. */
    val STOP_REFUSED = listOf("android", "com.android.systemui", "com.android.phone", "com.termux", "com.thomaswcode.phonerelay")
    private val SETTING_KEY_RE = Regex("^[\\w.:-]{1,100}$")
    val COMPONENT_RE = Regex("^[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)*/[A-Za-z0-9_.$]+$")
    private val SAFE_RE = Regex("^[\\w@%+=:,./-]+$")
    private val STAT_TYPES = mapOf("regular file" to "file", "regular empty file" to "file", "directory" to "dir", "symbolic link" to "link")

    /** Quoted for the shell as Python's shlex.quote does it, so both clients send the same command lines. */
    fun quote(s: String): String = if (s.isNotEmpty() && SAFE_RE.matches(s)) s else "'" + s.replace("'", "'\"'\"'") + "'"

    /** package/.Class as package/package.Class: Android's lists name a component either way. */
    fun fullComponent(component: String): String {
        val pkg = component.substringBefore('/')
        val cls = component.substringAfter('/', "")
        return if (cls.startsWith(".")) "$pkg/$pkg$cls" else component
    }

    // ---- accessibility ----

    const val ACCESSIBILITY = "settings get secure enabled_accessibility_services; echo $SPLIT_MARKER; dumpsys accessibility"

    /** Enabled, bound and missing (enabled, not bound) services; bound and missing are null when the dump names none. */
    class Accessibility(val enabled: List<String>, val bound: List<String>?, val missing: List<String>?)

    /**
     * The services enabled (the setting) and bound (`dumpsys accessibility`: from "Bound services:"
     * to "Enabled services:", each entry Service[label=..., id=package/.Class, ...], as Android 16
     * prints it). A dump that shows bound services without naming them gives null, not every service unbound.
     */
    fun parseAccessibility(out: String): Accessibility {
        val setting = out.substringBefore(SPLIT_MARKER).trim()
        val dump = out.substringAfter(SPLIT_MARKER, "")
        val enabled = if (setting.isEmpty() || setting == "null") emptyList() else setting.split(':').filter { it.isNotEmpty() }.map(::fullComponent)
        val section = mutableListOf<String>()
        var inside = false
        for (line in dump.lines()) {
            if (!inside && "Bound services:" in line) inside = true
            if (inside && "Enabled services:" in line) break
            if (inside) section += line
        }
        val text = section.joinToString("\n")
        val bound = Regex("\\bid=([^,\\s\\]}]+)").findAll(text).map { fullComponent(it.groupValues[1]) }.toList()
        if (bound.isEmpty() && "Service[" in text) return Accessibility(enabled, null, null)
        return Accessibility(enabled, bound, enabled.filter { it !in bound })
    }

    // ---- install ----

    /**
     * `pm install -r` (never -d) with the APK streamed in by the shell (-S its size): the system
     * server cannot open a file in shared storage itself (SELinux), so `pm install /sdcard/...` fails.
     */
    fun install(path: String) = quote(path).let { "cat $it | pm install -r -S $(stat -c %s $it) 2>&1 || true" }

    /** `pm install`'s error ("INSTALL_FAILED_...: its reason"), or null when it says Success. */
    fun parseInstall(out: String): String? {
        if (Regex("^Success\\s*$", RegexOption.MULTILINE).containsMatchIn(out)) return null
        Regex("Failure \\[([A-Z_]+)(?::\\s*(.*?))?\\]\\s*$", RegexOption.MULTILINE).find(out)?.let { m ->
            return m.groupValues[1] + if (m.groupValues[2].isNotEmpty()) ": ${m.groupValues[2]}" else ""
        }
        return out.trim().take(300).ifEmpty { "pm install printed nothing" }
    }

    // ---- settings ----

    fun settingsCommand(args: JsonObject): Pair<String, String> {
        val namespace = args.stringArg("namespace")!!
        if (namespace !in SETTINGS_NAMESPACES) throw CommandError("invalid_args", "namespace must be one of ${SETTINGS_NAMESPACES.joinToString(", ")}")
        val key = args.stringArg("key")!!
        if (!SETTING_KEY_RE.matches(key)) throw CommandError("invalid_args", "key must be a setting's name, e.g. stay_on_while_plugged_in")
        return namespace to key
    }

    fun settings(namespace: String, key: String) = "settings get $namespace $key; echo $SPLIT_MARKER; cmd uimode night"

    /** (the setting's value, null when unset; dark mode: yes, no, auto, ...). */
    fun parseSettings(out: String): Pair<String?, String?> {
        val value = out.substringBefore(SPLIT_MARKER).trimEnd('\r', '\n')
        val night = Regex("Night mode:\\s*(\\S+)").find(out.substringAfter(SPLIT_MARKER, ""))?.groupValues?.get(1)
        return (if (value == "null") null else value) to night
    }

    // ---- logcat ----

    /** The read-only `logcat -d` for /logcat's arguments, and its line cap (one line more is read, to tell whether the cap cut it). */
    fun logcatCommand(args: JsonObject): Pair<String, Int> {
        val tags = when (val t = args["tag"]) {
            null, is JsonNull -> null
            is JsonPrimitive -> listOf(t)
            is JsonArray -> t.toList()
            else -> emptyList()
        }?.map { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }
        if (tags != null && (tags.isEmpty() || tags.any { it == null || !LOG_TAG_RE.matches(it) })) {
            throw CommandError("invalid_args", "tag must be a log tag or a list of them")
        }
        val level = args.stringArg("level", required = false)
        if (level != null && level !in LOG_LEVELS) throw CommandError("invalid_args", "level must be one of ${LOG_LEVELS.joinToString(", ")}")
        val buffer = args.stringArg("buffer", required = false)
        if (buffer != null && buffer !in LOG_BUFFERS) throw CommandError("invalid_args", "buffer must be one of ${LOG_BUFFERS.joinToString(", ")}")
        val lines = when (val l = args["lines"]) {
            null, is JsonNull -> DEFAULT_LOG_LINES
            else -> (l as? JsonPrimitive)?.takeIf { !it.isString && it.content.toIntOrNull() != null }?.intOrNull?.takeIf { it > 0 }
                ?: throw CommandError("invalid_args", "lines must be a whole number above 0")
        }
        val parts = mutableListOf("logcat", "-d")
        if (buffer != null) parts += listOf("-b", buffer)
        val since = args["since"] as? JsonPrimitive
        if (since != null) {
            val seconds = since.takeIf { !it.isString }?.content?.toDoubleOrNull()
            when {
                // Seconds ago by the phone's own clock, in logcat's seconds.milliseconds form
                seconds != null && seconds > 0 && seconds.isFinite() -> parts += listOf("-t", "\"$(( $(date +%s) - ${Math.ceil(seconds).toLong()} )).000\"")
                since.isString && LOG_TIME_RE.matches(since.content) ->
                    parts += listOf("-t", quote(if ('.' in since.content) since.content else since.content + ".000"))
                else -> throw CommandError("invalid_args", "since must be seconds ago or a time, e.g. \"2026-11-09 06:58:42\"")
            }
        }
        if (tags != null) parts += listOf("-s") + tags.map { quote("$it:${level ?: "V"}") } // a tag may hold $
        else if (level != null) parts += "'*:$level'"
        return parts.joinToString(" ") + " 2>&1 | tail -n ${lines + 1}" to lines
    }

    class LogLines(val lines: List<String>, val truncated: Boolean)

    fun logcatResult(out: String, lines: Int): LogLines {
        val got = out.lines().map { it.trimEnd('\r') }.let { if (out.endsWith("\n")) it.dropLast(1) else it }.let { if (out.isEmpty()) emptyList() else it }
        return if (got.size > lines) LogLines(got.takeLast(lines), true) else LogLines(got, false)
    }

    // ---- an app's own files (run-as) ----

    /** A path inside an app's own data folder (run-as's working folder): relative, with no ".."; with `file`, a file's (no trailing /). */
    fun dataPath(args: JsonObject, required: Boolean = true, file: Boolean = false): String? {
        val v = args["path"]
        if ((v == null || v is JsonNull) && !required) return null
        val p = (v as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (p == null || !DATA_PATH_RE.matches(p) || ".." in p.split('/')) {
            throw CommandError("invalid_args", "path must be relative to the app's data folder, e.g. files/plan.json, with no ..")
        }
        if (file && p.endsWith("/")) throw CommandError("invalid_args", "path must name a file, not a folder")
        return p
    }

    /**
     * A folder of the app's own data as a tar on stdout. tar's messages are kept out of the archive's
     * bytes, and if it did not finish cleanly (a file it could not read was left out), TAR_FAILED_MARKER
     * follows the archive: a whole archive ends in zero blocks, never in that text.
     */
    fun runAsTar(pkg: String, path: String) = "run-as $pkg sh -c ${quote("tar -cf - ${quote(path)} 2>/dev/null || echo $TAR_FAILED_MARKER")}"

    /** A tar archive's first header (ustar), not an error printed in its place. */
    fun isTar(head: ByteArray): Boolean = head.size >= 512 && String(head, 257, 5, Charsets.US_ASCII) == "ustar"

    /** (the folder, the hidden .part file beside `path`) a write goes to before it replaces the file. */
    fun partPath(path: String): Pair<String, String> {
        val folder = path.substringBeforeLast('/', "").ifEmpty { "." }
        return folder to "$folder/.${path.substringAfterLast('/')}.part"
    }

    fun runAsWrite(pkg: String, folder: String, part: String) = "run-as $pkg sh -c ${quote("mkdir -p ${quote(folder)} && cat > ${quote(part)}")}"

    fun runAsSize(pkg: String, path: String) = "run-as $pkg stat -c %s -- ${quote(path)} 2>&1"

    fun runAsMove(pkg: String, from: String, to: String) = "run-as $pkg mv -f -- ${quote(from)} ${quote(to)} 2>&1"

    fun runAsDelete(pkg: String, path: String) = "run-as $pkg rm -f -- ${quote(path)} 2>&1"

    // ---- content query ----

    /** `content query` for /content's arguments (read-only), and its projection's columns. */
    fun contentCommand(args: JsonObject): Pair<String, List<String>?> {
        val uri = args.stringArg("uri")!!
        if (!CONTENT_URI_RE.matches(uri)) throw CommandError("invalid_args", "uri must be a content:// URI, e.g. content://com.android.calendar/events")
        val parts = mutableListOf("content", "query", "--uri", quote(uri))
        val projection = when (val p = args["projection"]) {
            null, is JsonNull -> null
            is JsonArray -> p.map { (it as? JsonPrimitive)?.takeIf { c -> c.isString && COLUMN_RE.matches(c.content) }?.content
                ?: throw CommandError("invalid_args", "projection must be a list of column names") }
            else -> throw CommandError("invalid_args", "projection must be a list of column names")
        }
        if (projection != null) {
            if (projection.isEmpty()) throw CommandError("invalid_args", "projection must be a list of column names")
            parts += listOf("--projection", projection.joinToString(":"))
        }
        args.stringArg("where", required = false)?.let { parts += listOf("--where", quote(it)) }
        args.stringArg("sort", required = false)?.let {
            if (!SORT_RE.matches(it)) throw CommandError("invalid_args", "sort must be columns, e.g. dtstart DESC")
            parts += listOf("--sort", quote(it))
        }
        return parts.joinToString(" ") + " 2>&1" to projection
    }

    /**
     * `content query`'s rows ("Row: 0 a=1, b=x, y"), each as column -> value, NULL as null. With a
     * projection the columns are found in their order, so a value holding ", " or newlines comes back
     * whole; without one, it is cut at each ", name=". The provider's errors go to stderr while
     * `content` still exits 0, so they are looked for here, before the first row only: a value's
     * later lines may start with anything ("Exception handling").
     */
    fun parseContent(out: String, projection: List<String>?): List<Map<String, String?>> {
        val beforeRows = if (out.startsWith("Row: ")) "" else out.substringBefore("\nRow: ")
        if (Regex("^(Error while accessing provider|Exception|java\\.|\\[ERROR\\])", RegexOption.MULTILINE).containsMatchIn(beforeRows)) {
            throw CommandError("content_failed", out.trim().take(300))
        }
        if (out.isBlank() || out.trim() == "No result found.") return emptyList()
        val records = mutableListOf<String>()
        for (line in out.lines()) {
            val m = Regex("^Row: \\d+ (.*)$").find(line)
            if (m != null) records += m.groupValues[1]
            else if (records.isNotEmpty()) records[records.lastIndex] = records.last() + "\n" + line
        }
        // `content` ends its output with a newline, which the last row's text would otherwise keep
        if (records.isNotEmpty() && out.endsWith("\n")) records[records.lastIndex] = records.last().removeSuffix("\n")
        return records.map { text ->
            val row = linkedMapOf<String, String?>()
            if (projection != null) {
                var rest = text
                projection.forEachIndexed { i, col ->
                    if (!rest.startsWith("$col=")) throw CommandError("content_failed", "unexpected row: ${text.take(200)}")
                    rest = rest.substring(col.length + 1)
                    val value = if (i + 1 < projection.size) {
                        val cut = rest.indexOf(", ${projection[i + 1]}=")
                        if (cut < 0) rest.also { rest = "" } else rest.substring(0, cut).also { rest = rest.substring(cut + 2) }
                    } else rest
                    row[col] = if (value == "NULL") null else value
                }
            } else {
                for (pair in text.split(Regex(", (?=\\w+=)"))) {
                    val value = pair.substringAfter('=', "")
                    row[pair.substringBefore('=')] = if (value == "NULL") null else value
                }
            }
            row
        }
    }

    fun nightMode(out: String): String? = Regex("Night mode:\\s*(\\S+)").find(out)?.groupValues?.get(1)

    /** A directory's entries (or one file) as `stat -c '%F|%s|%Y|%n'` lines; MISSING_MARKER if it cannot be read. */
    fun listCommand(path: String): String {
        val q = quote(path)
        val fmt = "'%F|%s|%Y|%n'"
        return "if [ -d $q ]; then cd $q 2>/dev/null || { echo $MISSING_MARKER; exit 0; }; " +
            "for f in * .*; do case \"\$f\" in .|..) continue;; esac; " +
            "[ -e \"\$f\" ] || [ -L \"\$f\" ] || continue; stat -c $fmt -- \"\$f\"; done; " +
            "else stat -c $fmt -- $q 2>/dev/null || echo $MISSING_MARKER; fi"
    }

    fun runAsList(pkg: String, path: String) = "run-as $pkg sh -c ${quote(listCommand(path))} 2>&1"

    fun runAsStat(pkg: String, path: String) = "run-as $pkg stat -c '%F|%s|%Y|%n' -- ${quote(path)} 2>/dev/null || echo $MISSING_MARKER"

    fun runAsCat(pkg: String, path: String) = "run-as $pkg cat ${quote(path)}"

    /** One `stat -c '%F|%s|%Y|%n'` line as an entry, or null. */
    fun parseStat(line: String): FileEntry? {
        val parts = line.trimEnd('\r').split('|', limit = 4)
        if (parts.size != 4 || parts[1].toLongOrNull() == null || parts[2].toLongOrNull() == null) return null
        return FileEntry(parts[3], STAT_TYPES[parts[0]] ?: parts[0], parts[1].toLong(), parts[2].toLong())
    }

    // ---- an intent sent as the shell user ----

    /** `am start` (or `am broadcast`) for an intent's parts, as the adb client builds it. */
    fun am(spec: IntentSpec): String {
        val parts = if (spec.broadcast) mutableListOf("am", "broadcast") else mutableListOf("am", "start", "-W")
        listOf("-a" to spec.action, "-d" to spec.data, "-t" to spec.type, "-n" to spec.component).forEach { (flag, v) ->
            if (v != null) parts += listOf(flag, quote(v))
        }
        spec.pkg?.let { parts += listOf("-p", it) }
        spec.categories.forEach { parts += listOf("-c", quote(it)) }
        spec.flags?.let { parts += listOf("-f", it.toString()) }
        for ((key, value) in spec.extras) {
            val k = quote(key)
            parts += when (val v = extraValue(key, value)) {
                null -> listOf("--esn", k)
                is Boolean -> listOf("--ez", k, v.toString())
                is Int -> listOf("--ei", k, v.toString())
                is Long -> listOf(if (v in Int.MIN_VALUE..Int.MAX_VALUE) "--ei" else "--el", k, v.toString())
                is Double -> listOf("--ef", k, v.toString())
                else -> listOf("--es", k, quote(v.toString()))
            }
        }
        return parts.joinToString(" ") + " || true"
    }
}
