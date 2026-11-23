package com.thomaswcode.phonerelay.commands

import org.junit.Assert.assertEquals
import org.junit.Test

class ShellStateTest {

    private val unasked: () -> Boolean = { throw AssertionError("asked when it needn't be") }

    private fun of(
        paired: Boolean = true,
        portOpen: () -> Boolean = { false },
        onWifi: Boolean = true,
        wirelessDebuggingOn: Boolean = true,
        answering: () -> Boolean = { true },
        canEnable: Boolean = false,
    ) = ShellState.of(paired, portOpen, onWifi, wirelessDebuggingOn, answering, canEnable)

    @Test
    fun notPairedTriesNothing() {
        assertEquals(ShellState(paired = false, reachable = false, via = null), of(paired = false, portOpen = unasked, answering = unasked))
    }

    @Test
    fun thePortForMobileDataOnAnyNetwork() {
        assertEquals(ShellState(paired = true, reachable = true, via = "port"), of(portOpen = { true }, onWifi = false, answering = unasked))
        assertEquals(ShellState(paired = true, reachable = true, via = "port"), of(portOpen = { true }, answering = unasked))
    }

    @Test
    fun offWifiWithoutThePortIsUnreachable() {
        assertEquals(ShellState(paired = true, reachable = false, via = null), of(onWifi = false, canEnable = true, answering = unasked))
    }

    @Test
    fun wirelessDebuggingCountsOnlyWhenItAnswers() {
        assertEquals(ShellState(paired = true, reachable = true, via = "wireless_debugging"), of())
        // On, but not answering: a network not allowed, or adbd not listening yet.
        assertEquals(ShellState(paired = true, reachable = false, via = null), of(answering = { false }))
        assertEquals(ShellState(paired = true, reachable = false, via = null), of(answering = { false }, canEnable = true))
    }

    @Test
    fun offOnWifiIsNotReachableThoughTheAppMaySwitchItOn() {
        // On Wi-Fi with wireless debugging off and the grant held: once reported reachable, though a
        // network not allowed left every command unanswered.
        assertEquals(
            ShellState(paired = true, reachable = false, via = null, canSwitchOn = true),
            of(wirelessDebuggingOn = false, canEnable = true, answering = unasked),
        )
        assertEquals(ShellState(paired = true, reachable = false, via = null), of(wirelessDebuggingOn = false, answering = unasked))
    }
}
