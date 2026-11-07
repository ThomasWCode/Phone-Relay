package com.thomaswcode.phonerelay.commands

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull

// Reading a command's args: the relay has checked them already; the app checks again, as the adb client does.

private val PACKAGE_RE = Regex("^[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)*$")

/** A command's deadline when the relay sends none (seconds), as the adb client's DEFAULT_TIMEOUT_S. */
const val DEFAULT_TIMEOUT_S = 30.0
const val TRANSFER_TIMEOUT_S = 300.0
/** How long a request waits for the owner's answer when the agent gives no `seconds`, as the relay's DEFAULT_REQUEST_S. */
const val DEFAULT_REQUEST_S = 300.0

fun JsonObject.number(name: String): Double? {
    val v = this[name] as? JsonPrimitive ?: return null
    if (v.isString) return null
    return v.doubleOrNull?.takeIf { it.isFinite() }
}

fun JsonObject.numberArg(name: String, default: Double, minimum: Double = 0.0): Double {
    if (this[name] == null || this[name] is JsonNull) return default
    val v = number(name) ?: throw CommandError("invalid_args", "$name must be a number")
    if (v < minimum) throw CommandError("invalid_args", "$name must be a number ${fmt(minimum)} or above")
    return v
}

fun JsonObject.requiredNumber(name: String): Double =
    number(name) ?: throw CommandError("invalid_args", "$name must be a number")

fun JsonObject.stringArg(name: String, required: Boolean = true): String? {
    val v = this[name]
    if ((v == null || v is JsonNull) && !required) return null
    val s = (v as? JsonPrimitive)?.takeIf { it.isString }?.content
    if (s.isNullOrEmpty()) throw CommandError("invalid_args", "$name must be a non-empty string")
    return s
}

fun JsonObject.boolArg(name: String, default: Boolean): Boolean {
    val v = this[name] ?: return default
    if (v is JsonNull) return default
    return (v as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull
        ?: throw CommandError("invalid_args", "$name must be true or false")
}

fun JsonObject.packageArg(name: String = "package", required: Boolean = false): String? {
    val v = stringArg(name, required) ?: return null
    if (!PACKAGE_RE.matches(v)) throw CommandError("invalid_args", "$name must be an Android package name")
    return v
}

fun JsonObject.scaleArg(default: Double = 1.0): Double {
    val v = numberArg("scale", default)
    if (v <= 0) throw CommandError("invalid_args", "scale must be a number above 0")
    return v
}

/** The packages named by if_foreground (one name or a list), or null. */
fun JsonObject.ifForeground(): List<String>? {
    val v = this["if_foreground"] ?: return null
    if (v is JsonNull) return null
    val names = when (v) {
        is JsonPrimitive -> listOf(v)
        is JsonArray -> v.toList()
        else -> emptyList()
    }.map { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }
    if (names.isEmpty() || names.any { it == null || !PACKAGE_RE.matches(it) }) {
        throw CommandError("invalid_args", "if_foreground must be a package name or a list of them")
    }
    return names.filterNotNull()
}

fun JsonObject.timeoutArg(default: Double): Double {
    val v = numberArg("timeout", default)
    if (v <= 0) throw CommandError("invalid_args", "timeout must be a number of seconds above 0")
    return v
}

/** Deadline when the agent sets none: as the adb client's default_timeout. */
fun defaultTimeout(cmd: String, args: JsonObject): Double = when (cmd) {
    "type" -> DEFAULT_TIMEOUT_S + ((args["text"] as? JsonPrimitive)?.content?.length ?: 0) / 10.0
    "swipe", "gesture" -> DEFAULT_TIMEOUT_S + maxOf(args.number("ms") ?: 0.0, 0.0) / 1000
    "wait" -> DEFAULT_TIMEOUT_S + (args.number("seconds") ?: 10.0)
    "pull", "push", "record_stop" -> TRANSFER_TIMEOUT_S
    // As the adb client: twice the recording's length, a minute on top (the consent prompt, the transfer).
    "record", "audio" -> 2 * DEFAULT_TIMEOUT_S + 2 * (args.number("seconds") ?: 10.0)
    "batch" -> DEFAULT_TIMEOUT_S + ((args["steps"] as? JsonArray) ?: JsonArray(emptyList())).sumOf { step ->
        val obj = step as? JsonObject
        val cmdName = (obj?.get("cmd") as? JsonPrimitive)?.content ?: ""
        val stepArgs = obj?.get("args") as? JsonObject ?: JsonObject(emptyMap())
        if (cmdName == "sleep") maxOf(stepArgs.number("ms") ?: 0.0, 0.0) / 1000
        else stepArgs.number("timeout")?.takeIf { it > 0 } ?: defaultTimeout(cmdName, stepArgs)
    }
    // The wait for the owner's answer, then the queued steps' own time (as the relay's).
    "request" -> (args.number("seconds") ?: DEFAULT_REQUEST_S) +
        if (args["steps"] is JsonArray) defaultTimeout("batch", args) else DEFAULT_TIMEOUT_S
    else -> DEFAULT_TIMEOUT_S
}

/** A JSON value as an intent extra's Kotlin value: String, Int or Long, Double, Boolean or null. */
fun extraValue(key: String, v: JsonElement): Any? = when {
    v is JsonNull -> null
    v is JsonPrimitive && v.isString -> v.content
    v is JsonPrimitive && v.booleanOrNull != null -> v.booleanOrNull
    v is JsonPrimitive && v.intOrNull != null -> v.intOrNull
    v is JsonPrimitive && v.content.toLongOrNull() != null -> v.content.toLong()
    v is JsonPrimitive && v.doubleOrNull != null -> v.doubleOrNull
    else -> throw CommandError("invalid_args", "extras.$key must be a string, number, boolean or null")
}

fun fmt(v: Double): String = if (v == Math.floor(v) && !v.isInfinite()) v.toLong().toString() else v.toString()
