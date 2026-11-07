package com.thomaswcode.phonerelay

import android.accessibilityservice.AccessibilityService
import android.annotation.SuppressLint
import android.graphics.PixelFormat
import android.os.SystemClock
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.KeyEvent
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import com.thomaswcode.phonerelay.commands.Answer

/**
 * The request's card at the top of the screen, drawn by the accessibility service as an accessibility
 * overlay: no "display over other apps" grant, and Android trusts its touches. Touches outside it go
 * through to the app beneath (FLAG_NOT_TOUCH_MODAL), so the owner can keep using the phone before
 * answering. It takes key focus only while the owner types a message, and gives it back at a touch
 * outside it or Back. Dragging the header moves it. Main thread only.
 */
class RequestCard(
    private val service: AccessibilityService,
    reason: String,
    steps: List<String>,
    private val deadline: Long,
    private val onAnswer: (Answer) -> Unit,
) {
    private val wm = service.getSystemService(WindowManager::class.java)
    private val themed = ContextThemeWrapper(service, android.R.style.Theme_DeviceDefault_DayNight)
    @SuppressLint("InflateParams") // a window's root: there is no parent to inflate it into
    private val root: View = LayoutInflater.from(themed).inflate(R.layout.request_card, null)
    private val message: EditText = root.findViewById(R.id.request_message)
    private val timer: TextView = root.findViewById(R.id.request_timer)
    private val accept: Button = root.findViewById(R.id.request_accept)
    private val deny: Button = root.findViewById(R.id.request_deny)
    private var attached = false
    private val density = service.resources.displayMetrics.density
    private val params = WindowManager.LayoutParams(
        width(),
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
            WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
        PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
        y = dp(44) // below the status bar
        softInputMode = WindowManager.LayoutParams.SOFT_INPUT_STATE_UNCHANGED
        title = "Phone Relay request"
    }
    private val tick = object : Runnable {
        override fun run() {
            val left = ((deadline - SystemClock.elapsedRealtime()) / 1000).coerceAtLeast(0)
            timer.text = service.getString(R.string.request_left, "%d:%02d".format(left / 60, left % 60))
            if (attached) root.postDelayed(this, 1000)
        }
    }

    init {
        fill(reason, steps)
        accept.setOnClickListener { answer(true) }
        deny.setOnClickListener { answer(false) }
        listen()
    }

    @SuppressLint("SetTextI18n") // the steps are Ask.describe's words, one to a line
    private fun fill(reason: String, steps: List<String>) {
        root.findViewById<TextView>(R.id.request_reason).text = reason
        if (steps.isNotEmpty()) {
            root.findViewById<TextView>(R.id.request_steps).apply {
                text = (listOf(service.getString(R.string.request_then)) + steps.map { "• $it" }).joinToString("\n")
                visibility = View.VISIBLE
            }
        }
    }

    @SuppressLint("ClickableViewAccessibility") // the touches only move focus and the card; the buttons stay buttons
    private fun listen() {
        // Typing a message needs key focus: taken on touching the box, given back on a touch elsewhere or Back.
        message.setOnTouchListener { _, e ->
            if (e.actionMasked == MotionEvent.ACTION_DOWN) focusable(true)
            false
        }
        root.setOnTouchListener { _, e ->
            if (e.actionMasked == MotionEvent.ACTION_OUTSIDE) focusable(false)
            false
        }
        message.setOnKeyListener { _, code, e ->
            if (code == KeyEvent.KEYCODE_BACK && e.action == KeyEvent.ACTION_UP) {
                focusable(false)
                true
            } else {
                false
            }
        }
        var downY = 0f
        var startY = 0
        root.findViewById<View>(R.id.request_header).setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downY = e.rawY
                    startY = params.y
                }
                MotionEvent.ACTION_MOVE -> {
                    params.y = (startY + (e.rawY - downY).toInt()).coerceIn(0, service.resources.displayMetrics.heightPixels - dp(120))
                    if (attached) wm.updateViewLayout(root, params)
                }
            }
            true
        }
    }

    fun show() {
        wm.addView(root, params)
        attached = true
        tick.run()
    }

    fun dismiss() {
        if (!attached) return
        attached = false
        root.removeCallbacks(tick)
        runCatching { themed.getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(message.windowToken, 0) }
        runCatching { wm.removeViewImmediate(root) }
    }

    private fun answer(accepted: Boolean) {
        accept.isEnabled = false
        deny.isEnabled = false
        onAnswer(Answer(accepted, message.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }))
    }

    private fun focusable(on: Boolean) {
        val flags = if (on) {
            params.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE.inv()
        } else {
            params.flags or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
        }
        if (flags == params.flags || !attached) return
        params.flags = flags
        wm.updateViewLayout(root, params)
        val imm = themed.getSystemService(InputMethodManager::class.java)
        if (on) {
            message.requestFocus()
            message.post { imm.showSoftInput(message, 0) }
        } else {
            imm.hideSoftInputFromWindow(message.windowToken, 0)
            message.clearFocus()
        }
    }

    private fun width(): Int {
        val screen = service.getSystemService(WindowManager::class.java).currentWindowMetrics.bounds.width()
        return minOf(screen - dp(24), dp(520))
    }

    private fun dp(v: Int) = (v * density).toInt()
}
