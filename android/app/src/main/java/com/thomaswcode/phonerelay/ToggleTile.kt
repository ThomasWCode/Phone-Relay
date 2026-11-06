package com.thomaswcode.phonerelay

import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** The Quick Settings tile: the owner's on/off switch for the relay, from anywhere on the phone. */
class ToggleTile : TileService() {
    override fun onStartListening() = refresh()

    override fun onClick() {
        val app = RelayApp.of(this)
        app.setEnabled(!app.prefs.enabled)
        refresh()
    }

    private fun refresh() {
        val tile = qsTile ?: return
        val app = RelayApp.of(this)
        tile.state = if (app.prefs.enabled) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.subtitle = if (app.prefs.enabled) app.connection.status.value.label else getString(R.string.off)
        tile.updateTile()
    }
}
