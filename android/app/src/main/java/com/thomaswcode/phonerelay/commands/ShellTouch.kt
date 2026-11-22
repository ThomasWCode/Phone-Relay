package com.thomaswcode.phonerelay.commands

import kotlin.math.roundToInt

/**
 * Touch through the shell (`input swipe`), for while ScreenLock is locked. Shell input enters Android after the
 * accessibility input chain, so it passes the lock without pausing it: a finger on the screen (a pocket) neither gets
 * through nor cancels the gesture. `input` moves one finger in a straight line, so only a tap, a swipe or a long press
 * goes this way; anything else stays an accessibility gesture.
 */
object ShellTouch {
    /** The command for one finger along at most two points (a tap is a swipe that stays put), or null if `input` can't do it. */
    fun command(paths: List<List<Point>>, ms: Long): String? {
        if (paths.size != 1) return null
        val points = paths[0]
        // Points off the screen's top or left edge are left to the gesture, which refuses them
        if (points.size !in 1..2 || points.any { it.x < 0 || it.y < 0 }) return null
        val a = points.first()
        val b = points.last()
        return "input swipe ${a.x.roundToInt()} ${a.y.roundToInt()} ${b.x.roundToInt()} ${b.y.roundToInt()} $ms"
    }
}
