package com.thomaswcode.phonerelay.commands

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ShellTouchTest {

    @Test
    fun aTapIsASwipeThatStaysPut() {
        assertEquals("input swipe 540 1170 540 1170 50", ShellTouch.command(listOf(listOf(Point(540.4f, 1169.6f))), 50))
    }

    @Test
    fun aStraightSwipeOrLongPress() {
        assertEquals("input swipe 1 2 3 4 300", ShellTouch.command(listOf(listOf(Point(1f, 2f), Point(3f, 4f))), 300))
        assertEquals("input swipe 9 9 9 9 800", ShellTouch.command(listOf(listOf(Point(9f, 9f), Point(9f, 9f))), 800))
    }

    @Test
    fun whatInputCannotDoIsLeftToTheGesture() {
        assertNull(ShellTouch.command(listOf(listOf(Point(1f, 1f)), listOf(Point(2f, 2f))), 300)) // two fingers
        assertNull(ShellTouch.command(listOf(listOf(Point(1f, 1f), Point(2f, 2f), Point(3f, 3f))), 300)) // a bent path
        assertNull(ShellTouch.command(listOf(listOf(Point(-1f, 5f))), 50)) // off the left edge
    }
}
