package com.thomaswcode.phonerelay.commands

/** A recording's frame size, as screenrecord picks it: the size asked for, else the screen's. */
object Video {
    /**
     * `wanted`, rounded down to the encoder's alignment; smaller in eighths, keeping its shape, if
     * the encoder cannot take it. Null if no size down to an eighth will do.
     */
    fun fit(wanted: Size, widthAlign: Int, heightAlign: Int, supported: (Int, Int) -> Boolean): Size? {
        for (eighths in 8 downTo 1) {
            val w = align(wanted.width * eighths / 8, widthAlign)
            val h = align(wanted.height * eighths / 8, heightAlign)
            if (w > 0 && h > 0 && supported(w, h)) return Size(w, h)
        }
        return null
    }

    private fun align(v: Int, to: Int): Int = v - v % maxOf(to, 2)
}
