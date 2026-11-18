#!/usr/bin/env python3
# ==============================================================================
# Miracast viewer with mouse and keyboard input (UIBC over HIDC)
#
# Started by scripts/miracast-player.sh, which miracle-sinkctl runs as its
# external player. Shows the received stream full screen and sends the laptop's
# mouse and keyboard to the phone as USB HID reports over the Wi-Fi Display
# User Input Back Channel (UIBC, HIDC category).
#
# Arguments as passed by miracle-sinkctl:
#   [<source-ip> <uibc-port>] [-d <level>] [-a] [-s WxH] -p <rtp-port> [-r WxH]
#   (the first two only when the phone enabled UIBC)
#
# Controls:
#   Click the picture        capture mouse and keyboard (they go to the phone)
#   Ctrl+Alt                 release mouse and keyboard back to the laptop
#   F11 (while released)     toggle full screen
#   Ctrl+Q (while released)  close the viewer and end the session
#
# Every 10 s a "stats:" line reports decoded frames per second, how much
# compressed video is waiting for the decoder (if this grows, the decoder is too
# slow and latency builds up), and how input packets are flowing to the phone.
# ==============================================================================
import argparse
import collections
import ctypes
import socket
import struct
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gst, GstVideo, Gtk  # noqa: E402,F401

APP_TITLE = "Miracast Receiver"


def log(msg):
    print("viewer: " + msg, flush=True)


# ------------------------------------------------------------------------------
# HID report descriptors (USB HID 1.11)
# ------------------------------------------------------------------------------
# Mouse: 5 buttons, relative X/Y/wheel (8-bit each), horizontal pan (8-bit).
# Report layout: [buttons, x, y, wheel, pan]. The first three bytes match the
# boot protocol mouse report.
MOUSE_DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,        # Generic Desktop, Mouse, Application
    0x09, 0x01, 0xA1, 0x00,                    # Pointer, Physical
    0x05, 0x09, 0x19, 0x01, 0x29, 0x05,        # Buttons 1..5
    0x15, 0x00, 0x25, 0x01, 0x95, 0x05, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x81, 0x01,        # 3 bits padding
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38,   # X, Y, Wheel
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
    0x05, 0x0C, 0x0A, 0x38, 0x02,              # Consumer, AC Pan
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
])

# Keyboard: boot protocol layout [modifiers, reserved, key1..key6], with key
# usages 0x00..0xFF so that media keys fit as well.
KEYBOARD_DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,        # Generic Desktop, Keyboard, Application
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7,        # Modifiers
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,        # Reserved byte
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,  # LEDs
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,        # LED padding
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00,
    0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00,  # Key array
    0xC0,
])

HID_TYPE_KEYBOARD = 0
HID_TYPE_MOUSE = 1
HID_USAGE_REPORT = 0
HID_USAGE_DESCRIPTOR = 1
INPUT_PATH_USB = 1
UIBC_CATEGORY_HIDC = 1

# HID usage (index) -> Linux evdev key code, from the kernel's usbkbd.c table.
_HID_TO_EVDEV = [
    0, 0, 0, 0, 30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,
    50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44, 2, 3,
    4, 5, 6, 7, 8, 9, 10, 11, 28, 1, 14, 15, 57, 12, 13, 26,
    27, 43, 43, 39, 40, 41, 51, 52, 53, 58, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 87, 88, 99, 70, 119, 110, 102, 104, 111, 107, 109, 106,
    105, 108, 103, 69, 98, 55, 74, 78, 96, 79, 80, 81, 75, 76, 77, 71,
    72, 73, 82, 83, 86, 127, 116, 117, 183, 184, 185, 186, 187, 188, 189, 190,
    191, 192, 193, 194, 134, 138, 130, 132, 128, 129, 131, 137, 133, 135, 136, 113,
    115, 114, 0, 0, 0, 121, 0, 89, 93, 124, 92, 94, 95, 0, 0, 0,
    122, 123, 90, 91, 85,
]
_MODIFIERS = {29: 0xE0, 42: 0xE1, 56: 0xE2, 125: 0xE3, 97: 0xE4, 54: 0xE5, 100: 0xE6, 126: 0xE7}

EVDEV_TO_HID = {}
for _usage, _code in enumerate(_HID_TO_EVDEV):
    if _code and _code not in EVDEV_TO_HID:
        EVDEV_TO_HID[_code] = _usage
EVDEV_TO_HID.update(_MODIFIERS)


def uibc_hidc_packet(hid_type, usage, payload):
    """Build one UIBC packet carrying one HIDC input body (WFD 1.0, 4.11)."""
    body = struct.pack(">BBBH", INPUT_PATH_USB, hid_type, usage, len(payload)) + payload
    total = 4 + len(body)
    # Version 0, no timestamp, reserved bits 0, input category HIDC.
    header = struct.pack(">BBH", 0x00, UIBC_CATEGORY_HIDC, total)
    return header + body


def clamp8(v):
    return max(-127, min(127, int(v)))


# ------------------------------------------------------------------------------
# UIBC connection
# ------------------------------------------------------------------------------
class Uibc:
    """Sends HIDC reports to the phone from a background thread.

    The GTK thread only queues reports, so a slow phone or Wi-Fi hiccup never
    stalls the window. While packets are backlogged, consecutive mouse movements
    with the same buttons are merged into one report, so the phone always gets
    the latest position instead of a growing queue of old ones.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.cond = threading.Condition()
        self.queue = collections.deque()
        self.closing = False
        self.failures = 0
        # statistics, reset by take_stats()
        self.sent = 0
        self.total_sent = 0
        self.merged = 0
        self.max_send_ms = 0.0
        self.max_backlog = 0
        if self.enabled:
            threading.Thread(target=self._run, name="uibc", daemon=True).start()

    @property
    def enabled(self):
        return bool(self.host and self.port)

    @property
    def connected(self):
        return self.sock is not None

    # --- called from the GTK thread -------------------------------------------
    def mouse(self, buttons, dx=0, dy=0, wheel=0, pan=0):
        if not self.connected:
            return
        with self.cond:
            last = self.queue[-1] if self.queue else None
            if (last is not None and last[0] == "mouse" and last[1] == buttons
                    and not wheel and not pan and not last[4] and not last[5]):
                last[2] += dx
                last[3] += dy
                self.merged += 1
            else:
                self.queue.append(["mouse", buttons, dx, dy, wheel, pan])
            self.max_backlog = max(self.max_backlog, len(self.queue))
            self.cond.notify()

    def keyboard(self, modifiers, keys):
        if not self.connected:
            return
        k = list(keys)[:6] + [0] * (6 - min(6, len(keys)))
        report = bytes([modifiers & 0xFF, 0] + k)
        with self.cond:
            self.queue.append(["raw", uibc_hidc_packet(HID_TYPE_KEYBOARD, HID_USAGE_REPORT, report)])
            self.max_backlog = max(self.max_backlog, len(self.queue))
            self.cond.notify()

    def take_stats(self):
        with self.cond:
            s = (self.sent, self.merged, self.max_send_ms, self.max_backlog)
            self.sent = self.merged = self.max_backlog = 0
            self.max_send_ms = 0.0
        return s

    def close(self):
        with self.cond:
            self.closing = True
            self.cond.notify()

    # --- background thread -----------------------------------------------------
    def _connect(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=2)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(2)
            s.sendall(uibc_hidc_packet(HID_TYPE_MOUSE, HID_USAGE_DESCRIPTOR, MOUSE_DESCRIPTOR))
            s.sendall(uibc_hidc_packet(HID_TYPE_KEYBOARD, HID_USAGE_DESCRIPTOR, KEYBOARD_DESCRIPTOR))
            self.sock = s
            log("UIBC connected to %s:%d" % (self.host, self.port))
        except OSError as e:
            self.failures += 1
            if self.failures <= 3 or self.failures % 10 == 0:
                log("UIBC connect failed (attempt %d): %s" % (self.failures, e))

    def _packets(self, item):
        if item[0] == "raw":
            return [item[1]]
        _, buttons, dx, dy, wheel, pan = item
        out = []
        dx, dy = int(dx), int(dy)
        while True:          # reports carry at most 127 counts per axis
            sx, sy = clamp8(dx), clamp8(dy)
            report = struct.pack(">Bbbbb", buttons & 0x1F, sx, sy, clamp8(wheel), clamp8(pan))
            out.append(uibc_hidc_packet(HID_TYPE_MOUSE, HID_USAGE_REPORT, report))
            dx, dy, wheel, pan = dx - sx, dy - sy, 0, 0
            if dx == 0 and dy == 0:
                return out

    def _run(self):
        while True:
            if self.sock is None:
                with self.cond:
                    if self.closing:
                        return
                    self.queue.clear()       # never replay stale input after a reconnect
                self._connect()
                if self.sock is None:
                    with self.cond:
                        self.cond.wait(2)
                    continue
            with self.cond:
                while not self.queue and not self.closing:
                    self.cond.wait()
                if self.closing:
                    break
                item = self.queue.popleft()
            for packet in self._packets(item):
                t0 = time.monotonic()
                try:
                    self.sock.sendall(packet)
                except OSError as e:
                    log("UIBC send failed after %d packets: %s" % (self.total_sent, e))
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
                    break
                ms = (time.monotonic() - t0) * 1000.0
                with self.cond:
                    self.sent += 1
                    self.total_sent += 1
                    self.max_send_ms = max(self.max_send_ms, ms)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def set_bypass_compositor(xid):
    """Ask the compositor to leave this window alone (_NET_WM_BYPASS_COMPOSITOR).

    Saves a frame of latency with a compositing window manager. Best effort:
    any failure is ignored.
    """
    try:
        x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return False
        atom = x11.XInternAtom(dpy, b"_NET_WM_BYPASS_COMPOSITOR", 0)
        value = ctypes.c_long(1)
        x11.XChangeProperty(dpy, xid, atom, 6, 32, 0, ctypes.byref(value), 1)  # XA_CARDINAL, replace
        x11.XCloseDisplay(dpy)     # flushes the request
        return True
    except (OSError, AttributeError):
        return False


BUTTON_BITS = {1: 0x01, 3: 0x02, 2: 0x04, 8: 0x08, 9: 0x10}
HW_DECODERS = ("vah264dec", "vaapih264dec")


# ------------------------------------------------------------------------------
# Viewer window
# ------------------------------------------------------------------------------
class Viewer:
    def __init__(self, args):
        self.args = args
        self.uibc = Uibc(args.host, args.uibc_port)
        self.captured = False
        self.fullscreen = not args.windowed
        self.buttons = 0
        self.pressed = []          # HID key usages currently down, in order
        self.modifiers = 0
        self.scroll_acc_y = 0.0
        self.scroll_acc_x = 0.0
        self.acc_x = 0.0
        self.acc_y = 0.0
        self.last_pos = None       # last pointer position seen while captured
        self.warp_target = None    # pointer position we warped to, until its event arrives
        self.warp_time = 0.0
        self.xid = None
        self.sink_widget = None
        self.pipeline = None
        self.frames = 0
        self.decoder = None
        self.fell_back = False

        self.window = Gtk.Window(title=APP_TITLE)
        self.window.set_default_size(1280, 720)
        self.window.connect("destroy", self.on_destroy)
        self.window.connect("key-press-event", self.on_key)
        self.window.connect("key-release-event", self.on_key)
        self.window.connect("focus-out-event", self.on_focus_out)
        self.window.connect("focus-in-event", self.on_focus_in)
        css = Gtk.CssProvider()
        css.load_from_data(b"window, eventbox, drawingarea { background-color: #000000; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        sink_name, sink = self.pick_sink()
        log("video sink: %s" % sink_name)
        self.sink_name = sink_name
        if self.sink_widget is not None:
            self.area = Gtk.EventBox()
            self.area.add(self.sink_widget)
        else:
            self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.area.connect("realize", self.on_realize)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("button-press-event", self.on_button)
        self.area.connect("button-release-event", self.on_button)
        self.area.connect("scroll-event", self.on_scroll)
        self.window.add(self.area)
        self.first_sink = sink

    # --- GStreamer -------------------------------------------------------------
    def pick_sink(self):
        if self.args.sink in ("auto", "xvimagesink"):
            xv = Gst.ElementFactory.make("xvimagesink", None)
            if xv is not None and xv.set_state(Gst.State.READY) == Gst.StateChangeReturn.SUCCESS:
                xv.set_state(Gst.State.NULL)
                xv.set_property("force-aspect-ratio", True)
                xv.set_property("handle-events", False)
                return "xvimagesink", xv
            if self.args.sink == "xvimagesink":
                log("xvimagesink is not usable on this display")
        if self.args.sink in ("auto", "gtksink") and self.sink_widget is None:
            gtk = Gst.ElementFactory.make("gtksink", None)
            if gtk is not None:
                self.sink_widget = gtk.get_property("widget")
                return "gtksink", gtk
        x = Gst.ElementFactory.make("ximagesink", None)
        x.set_property("force-aspect-ratio", True)
        x.set_property("handle-events", False)
        return "ximagesink", x

    def pick_decoder(self):
        if self.args.decoder != "auto":
            return self.args.decoder
        for name in HW_DECODERS:
            if Gst.ElementFactory.find(name) is not None:
                return name
        return "avdec_h264"

    def build_pipeline(self, decoder, sink):
        sink.set_property("sync", False)    # show each frame as soon as it is decoded
        # The decoded-frame queue is leaky and two frames deep: if drawing falls
        # behind, old frames are dropped instead of adding delay. Compressed data
        # is never dropped, because that would corrupt the picture until the
        # next key frame.
        desc = ("udpsrc port=%d buffer-size=4194304 caps=\"application/x-rtp, media=video\" "
                "! rtpjitterbuffer latency=%d "
                "! rtpmp2tdepay ! tsdemux name=demux latency=%d "
                "demux. ! queue name=vqueue max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
                "! h264parse ! %s name=vdec "
                "! queue name=dqueue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream "
                "! videoconvert ! videoscale name=vscale "
                % (self.args.port, self.args.latency, self.args.demux_latency, decoder))
        if self.args.audio:
            desc += ("demux. ! queue max-size-buffers=0 max-size-time=0 ! aacparse ! avdec_aac "
                     "! audioconvert ! audioresample ! autoaudiosink ")
        pipeline = Gst.parse_launch(desc)
        pipeline.add(sink)
        pipeline.get_by_name("vscale").link(sink)
        pipeline.get_by_name("vdec").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self.on_frame)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_error)
        bus.connect("message::warning", self.on_warning)
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self.on_sync_message)
        return pipeline

    def start_pipeline(self, decoder, sink):
        self.decoder = decoder
        log("decoder: %s" % decoder)
        try:
            self.pipeline = self.build_pipeline(decoder, sink)
        except GLib.Error as e:
            if decoder == "avdec_h264" or self.fell_back:
                raise
            log("could not build the pipeline with %s: %s" % (decoder, e.message))
            self.fell_back = True
            _, sink = self.pick_sink()
            return self.start_pipeline("avdec_h264", sink)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE \
                and decoder != "avdec_h264" and not self.fell_back:
            log("%s failed to start" % decoder)
            self.fell_back = True
            GLib.idle_add(self.fall_back_to_software)

    def on_frame(self, pad, info):
        self.frames += 1
        return Gst.PadProbeReturn.OK

    def on_sync_message(self, bus, msg):
        if GstVideo.is_video_overlay_prepare_window_handle_message(msg) and self.xid:
            msg.src.set_window_handle(self.xid)

    def on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log("GStreamer error from %s: %s" % (msg.src.get_name(), err.message))
        # A hardware decoder that fails is replaced by the software decoder once.
        if (self.decoder != "avdec_h264" and not self.fell_back
                and self.sink_widget is None and self.args.decoder == "auto"):
            self.fell_back = True
            GLib.idle_add(self.fall_back_to_software)

    def fall_back_to_software(self):
        log("falling back to the software decoder avdec_h264")
        self.pipeline.set_state(Gst.State.NULL)
        _, sink = self.pick_sink()
        self.start_pipeline("avdec_h264", sink)
        return False

    def on_warning(self, bus, msg):
        err, dbg = msg.parse_warning()
        log("GStreamer warning from %s: %s" % (msg.src.get_name(), err.message))

    def log_stats(self):
        frames, self.frames = self.frames, 0
        backlog = -1
        q = self.pipeline.get_by_name("vqueue") if self.pipeline else None
        if q is not None:
            backlog = q.get_property("current-level-time") // 1000000
        sent, merged, max_ms, max_q = self.uibc.take_stats()
        line = "stats: %.1f fps decoded (%s), decode backlog %d ms" % (
            frames / self.args.stats_interval, self.decoder, backlog)
        if self.uibc.enabled:
            line += ", input %d packets (%d merged), slowest send %.0f ms, max queue %d" % (
                sent, merged, max_ms, max_q)
        log(line)
        return True

    # --- Window ----------------------------------------------------------------
    def on_realize(self, widget):
        # Without this GTK merges motion events, which can swallow the event
        # produced by re-centring the pointer and make movements jump or vanish.
        widget.get_window().set_event_compression(False)

    def apply_fullscreen(self):
        if self.fullscreen:
            self.window.set_keep_above(True)
            self.window.fullscreen()
        else:
            self.window.set_keep_above(False)
            self.window.unfullscreen()

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.apply_fullscreen()

    def on_focus_in(self, widget, event):
        if self.fullscreen:
            self.apply_fullscreen()
        return False

    def notify(self, text, popup=False):
        self.window.set_title("%s - %s" % (APP_TITLE, text))
        log(text)
        if popup:
            try:
                subprocess.Popen(["notify-send", "--expire-time=4000", APP_TITLE, text],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    # --- Capture handling ------------------------------------------------------
    def capture(self):
        if self.captured:
            return
        if not self.uibc.connected:
            self.notify("Input channel to the phone is not connected (see the session report)", popup=True)
            return
        gdkwin = self.area.get_window()
        display = gdkwin.get_display()
        seat = display.get_default_seat()
        cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.BLANK_CURSOR)
        status = seat.grab(gdkwin, Gdk.SeatCapabilities.ALL, True, cursor, None, None, None)
        if status != Gdk.GrabStatus.SUCCESS:
            self.notify("Could not capture mouse and keyboard (%s)" % status.value_nick, popup=True)
            return
        self.captured = True
        self.last_pos = None
        self.warp_target = None
        self.acc_x = self.acc_y = 0.0
        self.warp_to_center()
        self.notify("Controlling the phone. Press Ctrl+Alt to release")

    def release(self, reason="Released. Click the picture to control the phone"):
        if not self.captured:
            return
        self.captured = False
        self.buttons = 0
        self.pressed = []
        self.modifiers = 0
        self.uibc.mouse(0)
        self.uibc.keyboard(0, [])
        self.window.get_display().get_default_seat().ungrab()
        self.notify(reason + ". F11: full screen, Ctrl+Q: quit", popup=True)

    def warp_to_center(self):
        alloc = self.area.get_allocation()
        cx, cy = alloc.width // 2, alloc.height // 2
        gdkwin = self.area.get_window()
        origin = gdkwin.get_origin()
        ox, oy = origin[-2], origin[-1]
        pointer = gdkwin.get_display().get_default_seat().get_pointer()
        pointer.warp(gdkwin.get_screen(), ox + cx, oy + cy)
        self.warp_target = (cx, cy)
        self.warp_time = time.monotonic()

    # --- Input events ----------------------------------------------------------
    def on_motion(self, widget, event):
        if not self.captured:
            return False
        x, y = event.x, event.y
        if self.warp_target is not None:
            if (int(round(x)), int(round(y))) == self.warp_target:
                # The event caused by our own warp: new reference point, no movement.
                self.last_pos = self.warp_target
                self.warp_target = None
                return True
            if time.monotonic() - self.warp_time > 0.25:
                self.warp_target = None   # warp event never arrived; resynchronise
                self.last_pos = None
        if self.last_pos is None:
            self.last_pos = (x, y)
            return True
        # Events queued before the warp still use the old reference point.
        self.acc_x += (x - self.last_pos[0]) * self.args.mouse_speed
        self.acc_y += (y - self.last_pos[1]) * self.args.mouse_speed
        self.last_pos = (x, y)
        dx, dy = int(self.acc_x), int(self.acc_y)
        if dx or dy:
            self.acc_x -= dx
            self.acc_y -= dy
            self.uibc.mouse(self.buttons, dx, dy)
        # Re-centre only when the pointer gets near an edge, not on every event.
        alloc = self.area.get_allocation()
        if self.warp_target is None and (
                abs(x - alloc.width / 2) > alloc.width / 4 or abs(y - alloc.height / 2) > alloc.height / 4):
            self.warp_to_center()
        return True

    def on_button(self, widget, event):
        if event.type not in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            return True                      # ignore double/triple-click synthesis
        if not self.captured:
            if event.type == Gdk.EventType.BUTTON_RELEASE and event.button == 1:
                self.capture()
            return True
        bit = BUTTON_BITS.get(event.button, 0)
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self.buttons |= bit
        else:
            self.buttons &= ~bit
        self.uibc.mouse(self.buttons)
        return True

    def on_scroll(self, widget, event):
        if not self.captured:
            return True
        wheel = pan = 0
        d = event.direction
        if d == Gdk.ScrollDirection.UP:
            wheel = 1
        elif d == Gdk.ScrollDirection.DOWN:
            wheel = -1
        elif d == Gdk.ScrollDirection.LEFT:
            pan = -1
        elif d == Gdk.ScrollDirection.RIGHT:
            pan = 1
        elif d == Gdk.ScrollDirection.SMOOTH:
            ok, sx, sy = event.get_scroll_deltas()
            self.scroll_acc_y -= sy
            self.scroll_acc_x += sx
            wheel = int(self.scroll_acc_y)
            pan = int(self.scroll_acc_x)
            self.scroll_acc_y -= wheel
            self.scroll_acc_x -= pan
        if wheel or pan:
            self.uibc.mouse(self.buttons, wheel=wheel, pan=pan)
        return True

    def on_key(self, widget, event):
        press = event.type == Gdk.EventType.KEY_PRESS
        if not self.captured:
            if press and event.keyval == Gdk.KEY_F11:
                self.toggle_fullscreen()
            elif press and event.keyval in (Gdk.KEY_q, Gdk.KEY_Q) and \
                    event.state & Gdk.ModifierType.CONTROL_MASK:
                self.window.destroy()
            return True
        usage = EVDEV_TO_HID.get(event.hardware_keycode - 8)
        if usage is None:
            return True
        if 0xE0 <= usage <= 0xE7:
            bit = 1 << (usage - 0xE0)
            self.modifiers = (self.modifiers | bit) if press else (self.modifiers & ~bit)
            ctrl = self.modifiers & 0x11
            alt = self.modifiers & 0x44
            if press and ctrl and alt:
                self.release()
                return True
        elif press:
            if usage in self.pressed:
                return True                  # X11 auto-repeat; the phone repeats itself
            self.pressed.append(usage)
        elif usage in self.pressed:
            self.pressed.remove(usage)
        self.uibc.keyboard(self.modifiers, self.pressed)
        return True

    def on_focus_out(self, widget, event):
        if self.captured:
            self.release("Focus lost, input released")
        return False

    # --- Lifecycle -------------------------------------------------------------
    def run(self):
        self.window.show_all()
        self.apply_fullscreen()
        gdkwin = self.window.get_window()
        try:
            self.window.present_with_time(GdkX11.x11_get_server_time(gdkwin))
        except (TypeError, AttributeError):
            self.window.present()
        if set_bypass_compositor(gdkwin.get_xid()):
            log("asked the compositor to bypass the viewer window")
        if self.sink_widget is None:
            self.xid = self.area.get_window().get_xid()
        self.start_pipeline(self.pick_decoder(), self.first_sink)
        GLib.timeout_add_seconds(self.args.stats_interval, self.log_stats)
        if self.uibc.enabled:
            hint = "Click the picture to control the phone. Ctrl+Alt releases"
        else:
            hint = "The phone did not enable input (UIBC). Video only. Ctrl+Q quits"
            log("UIBC not offered by the phone; running without input")
        GLib.timeout_add(1500, lambda: self.notify(hint, popup=True) and False)
        Gtk.main()

    def on_destroy(self, *_):
        if self.captured:
            self.release("Closing")
        self.log_stats()
        self.uibc.close()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        log("closed by user after %d UIBC packets" % self.uibc.total_sent)
        Gtk.main_quit()


def parse_args(argv):
    p = argparse.ArgumentParser(description="Miracast viewer with UIBC input")
    p.add_argument("host", nargs="?", help="source (phone) IP address for UIBC")
    p.add_argument("uibc_port", nargs="?", type=int, help="UIBC TCP port on the source")
    p.add_argument("-p", "--port", type=int, default=7236, help="RTP port")
    p.add_argument("-a", "--audio", action="store_true", help="play audio")
    p.add_argument("-d", "--debug", help="GStreamer debug level")
    p.add_argument("-s", "--scale", help="ignored (window scales automatically)")
    p.add_argument("-r", "--resolution", help="negotiated resolution (informational)")
    p.add_argument("--latency", type=int, default=50, help="RTP jitter buffer in ms (default 50)")
    p.add_argument("--demux-latency", type=int, default=100, help="MPEG-TS demuxer latency in ms")
    p.add_argument("--decoder", default="auto",
                   help="H.264 decoder: auto (hardware if available), vah264dec, vaapih264dec, avdec_h264")
    p.add_argument("--sink", default="auto", choices=["auto", "xvimagesink", "gtksink", "ximagesink"])
    p.add_argument("--mouse-speed", type=float, default=1.0, help="mouse movement multiplier")
    p.add_argument("--windowed", action="store_true", help="start in a window instead of full screen")
    p.add_argument("--stats-interval", type=int, default=10, help="seconds between stats log lines")
    return p.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])
    Gst.init(None)
    if args.debug:
        Gst.debug_set_active(True)
        Gst.debug_set_threshold_from_string(args.debug, True)
    log("start: rtp port %d, audio %s, resolution %s, UIBC %s" % (
        args.port, "on" if args.audio else "off", args.resolution or "unknown",
        "port %s" % args.uibc_port if args.uibc_port else "not offered"))
    Viewer(args).run()


if __name__ == "__main__":
    main()
