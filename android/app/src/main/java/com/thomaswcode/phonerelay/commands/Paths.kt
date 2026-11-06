package com.thomaswcode.phonerelay.commands

/**
 * Pointer paths over time, as the adb client's helper samples them: a path of points is walked at
 * an even pace by point, so fraction t of the duration is t of the way through its point list.
 * Android limits one gesture to a minute; a longer one is sent as consecutive continued strokes,
 * each a slice of every path.
 */
object Paths {
    /** The point at fraction t (0-1) of the way along `points`. */
    fun at(points: List<Point>, t: Double): Point {
        if (points.size == 1) return points[0]
        val pos = t.coerceIn(0.0, 1.0) * (points.size - 1)
        val k = minOf(pos.toInt(), points.size - 2)
        val f = (pos - k).toFloat()
        val a = points[k]
        val b = points[k + 1]
        return Point(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f)
    }

    /** The part of `points` between fractions t0 and t1: its two ends, and the corners in between. */
    fun slice(points: List<Point>, t0: Double, t1: Double): List<Point> {
        if (points.size == 1) return points
        val last = points.size - 1
        val inner = (1 until last).filter { i -> i.toDouble() / last > t0 && i.toDouble() / last < t1 }.map { points[it] }
        return listOf(at(points, t0)) + inner + listOf(at(points, t1))
    }

    /** How many slices a gesture of `ms` needs, at most `maxMs` each. */
    fun slices(ms: Long, maxMs: Long): Int = maxOf(1, ((ms + maxMs - 1) / maxMs).toInt())
}
