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
# ==============================================================================
import argparse
import socket
import struct
import subprocess
import sys

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
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.packets = 0
        self.failures = 0

    @property
    def enabled(self):
        return bool(self.host and self.port)

    @property
    def connected(self):
        return self.sock is not None

    def connect(self):
        if not self.enabled or self.sock:
            return self.connected
        try:
            s = socket.create_connection((self.host, self.port), timeout=2)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(1)
            self.sock = s
            log("UIBC connected to %s:%d" % (self.host, self.port))
            self.send(uibc_hidc_packet(HID_TYPE_MOUSE, HID_USAGE_DESCRIPTOR, MOUSE_DESCRIPTOR))
            self.send(uibc_hidc_packet(HID_TYPE_KEYBOARD, HID_USAGE_DESCRIPTOR, KEYBOARD_DESCRIPTOR))
        except OSError as e:
            self.failures += 1
            if self.failures <= 3 or self.failures % 10 == 0:
                log("UIBC connect failed (attempt %d): %s" % (self.failures, e))
            self.sock = None
        return self.connected

    def send(self, packet):
        if not self.sock:
            return
        try:
            self.sock.sendall(packet)
            self.packets += 1
        except OSError as e:
            log("UIBC send failed after %d packets: %s" % (self.packets, e))
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def mouse(self, buttons, dx=0, dy=0, wheel=0, pan=0):
        # Split large movements into several reports of at most 127 counts.
        while True:
            sx, sy = clamp8(dx), clamp8(dy)
            report = struct.pack(">Bbbbb", buttons & 0x1F, sx, sy, clamp8(wheel), clamp8(pan))
            self.send(uibc_hidc_packet(HID_TYPE_MOUSE, HID_USAGE_REPORT, report))
            dx, dy, wheel, pan = dx - sx, dy - sy, 0, 0
            if dx == 0 and dy == 0:
                break

    def keyboard(self, modifiers, keys):
        k = list(keys)[:6] + [0] * (6 - min(6, len(keys)))
        report = bytes([modifiers & 0xFF, 0] + k)
        self.send(uibc_hidc_packet(HID_TYPE_KEYBOARD, HID_USAGE_REPORT, report))


# ------------------------------------------------------------------------------
# Viewer window
# ------------------------------------------------------------------------------
BUTTON_BITS = {1: 0x01, 3: 0x02, 2: 0x04, 8: 0x08, 9: 0x10}


class Viewer:
    def __init__(self, args):
        self.args = args
        self.uibc = Uibc(args.host, args.uibc_port)
        self.captured = False
        self.fullscreen = True
        self.buttons = 0
        self.pressed = []          # HID key usages currently down, in order
        self.modifiers = 0
        self.scroll_acc_y = 0.0
        self.scroll_acc_x = 0.0
        self.center = (0, 0)
        self.xid = None
        self.sink_widget = None

        self.window = Gtk.Window(title=APP_TITLE)
        self.window.set_default_size(1280, 720)
        self.window.connect("destroy", self.on_destroy)
        self.window.connect("key-press-event", self.on_key)
        self.window.connect("key-release-event", self.on_key)
        self.window.connect("focus-out-event", self.on_focus_out)
        css = Gtk.CssProvider()
        css.load_from_data(b"window, eventbox, drawingarea { background-color: #000000; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.pipeline, video_sink = self.build_pipeline()
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
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("button-press-event", self.on_button)
        self.area.connect("button-release-event", self.on_button)
        self.area.connect("scroll-event", self.on_scroll)
        self.window.add(self.area)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_error)
        bus.connect("message::warning", self.on_warning)
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self.on_sync_message)

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
        if self.args.sink in ("auto", "gtksink"):
            gtk = Gst.ElementFactory.make("gtksink", None)
            if gtk is not None:
                self.sink_widget = gtk.get_property("widget")
                return "gtksink", gtk
        x = Gst.ElementFactory.make("ximagesink", None)
        x.set_property("force-aspect-ratio", True)
        x.set_property("handle-events", False)
        return "ximagesink", x

    def build_pipeline(self):
        name, sink = self.pick_sink()
        log("video sink: %s" % name)
        sink.set_property("sync", False)    # lowest latency for interactive use
        desc = ("udpsrc port=%d buffer-size=4194304 caps=\"application/x-rtp, media=video\" "
                "! rtpjitterbuffer latency=%d ! rtpmp2tdepay ! tsdemux name=demux "
                "demux. ! queue max-size-buffers=0 max-size-time=0 ! h264parse "
                "! %s ! videoconvert ! videoscale name=vscale "
                % (self.args.port, self.args.latency, self.args.decoder))
        if self.args.audio:
            desc += ("demux. ! queue max-size-buffers=0 max-size-time=0 ! aacparse ! avdec_aac "
                     "! audioconvert ! audioresample ! autoaudiosink ")
        pipeline = Gst.parse_launch(desc)
        pipeline.add(sink)
        pipeline.get_by_name("vscale").link(sink)
        return pipeline, sink

    def on_sync_message(self, bus, msg):
        if GstVideo.is_video_overlay_prepare_window_handle_message(msg) and self.xid:
            msg.src.set_window_handle(self.xid)

    def on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log("GStreamer error: %s (%s)" % (err.message, dbg))

    def on_warning(self, bus, msg):
        err, dbg = msg.parse_warning()
        log("GStreamer warning: %s" % err.message)

    # --- Capture handling ------------------------------------------------------
    def notify(self, text):
        self.window.set_title("%s - %s" % (APP_TITLE, text))
        log(text)
        try:
            subprocess.Popen(["notify-send", "--expire-time=4000", APP_TITLE, text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def capture(self):
        if self.captured:
            return
        if not self.uibc.connected and not self.uibc.connect():
            self.notify("No input channel to the phone (see the session report)")
            return
        gdkwin = self.area.get_window()
        display = gdkwin.get_display()
        seat = display.get_default_seat()
        cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.BLANK_CURSOR)
        status = seat.grab(gdkwin, Gdk.SeatCapabilities.ALL, True, cursor, None, None, None)
        if status != Gdk.GrabStatus.SUCCESS:
            self.notify("Could not capture mouse and keyboard (%s)" % status.value_nick)
            return
        self.captured = True
        self.recenter()
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
        self.notify(reason + ". F11: full screen, Ctrl+Q: quit")

    def recenter(self):
        alloc = self.area.get_allocation()
        cx, cy = alloc.width // 2, alloc.height // 2
        self.center = (cx, cy)
        gdkwin = self.area.get_window()
        origin = gdkwin.get_origin()
        ox, oy = origin[-2], origin[-1]
        pointer = gdkwin.get_display().get_default_seat().get_pointer()
        pointer.warp(gdkwin.get_screen(), ox + cx, oy + cy)

    # --- Input events ----------------------------------------------------------
    def on_motion(self, widget, event):
        if not self.captured:
            return False
        dx = int(round(event.x)) - self.center[0]
        dy = int(round(event.y)) - self.center[1]
        if dx or dy:
            self.uibc.mouse(self.buttons, dx, dy)
            self.recenter()
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

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.window.fullscreen()
        else:
            self.window.unfullscreen()

    # --- Lifecycle -------------------------------------------------------------
    def retry_uibc(self):
        if self.uibc.enabled and not self.uibc.connected:
            self.uibc.connect()
        return True

    def run(self):
        self.window.show_all()
        if self.fullscreen:
            self.window.fullscreen()
        if self.sink_widget is None:
            self.xid = self.area.get_window().get_xid()
        self.pipeline.set_state(Gst.State.PLAYING)
        if self.uibc.enabled:
            self.uibc.connect()
            GLib.timeout_add_seconds(2, self.retry_uibc)
            hint = "Click the picture to control the phone. Ctrl+Alt releases"
        else:
            hint = "The phone did not enable input (UIBC). Video only. Ctrl+Q quits"
            log("UIBC not offered by the phone; running without input")
        GLib.timeout_add(1500, lambda: self.notify(hint) and False)
        Gtk.main()

    def on_destroy(self, *_):
        if self.captured:
            self.release("Closing")
        self.pipeline.set_state(Gst.State.NULL)
        log("closed by user after %d UIBC packets" % self.uibc.packets)
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
    p.add_argument("--latency", type=int, default=50, help="jitter buffer in ms")
    p.add_argument("--decoder", default="avdec_h264", help="H.264 decoder element")
    p.add_argument("--sink", default="auto", choices=["auto", "xvimagesink", "gtksink", "ximagesink"])
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
