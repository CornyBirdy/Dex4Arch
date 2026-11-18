# Wireless DeX Research Spike (Experimental)

**Goal:** Tap Wireless DeX on a Samsung phone and get DeX on a Linux PC, with the PC acting as a Miracast receiver (Wi-Fi Direct sink).

For a single guided test run, follow `docs/MIRACAST_BETA_GUIDE.md`.

**Status: unproven.** On the test laptop (Qualcomm Atheros QCA9377, `ath10k_pci`) the phone has not yet discovered the PC. Nothing in this document is confirmed to work end to end.

Work is ordered in two stages, and the second does not start before the first works:

1. **Plain Miracast.** Smart View discovers the PC, connects, and a mirrored picture is shown.
2. **DeX over Miracast.** Check whether the Wireless DeX page lists the same receiver and whether the phone renders a DeX desktop or only mirrors.

## How MiracleCast drives the adapter

These points come from reading the MiracleCast source at commit `0b7f1f1` (the version packaged as `miraclecast-git 350.0b7f1f1`), not from a successful run.

- `miracle-wifid` starts its own `wpa_supplicant` on the interface (`src/wifi/wifid-supplicant.c`, `supplicant_run`). The upstream README says to stop NetworkManager and the system `wpa_supplicant` first. Two `wpa_supplicant` instances cannot both own the adapter's single P2P device.
- The output of that `wpa_supplicant` is redirected to the journal under the identifier `miracle-wifid-<interface>-<ifindex>`. `miracle-wifid` itself logs only at `notice` level by default, so a redirected log file stays empty unless `--log-level debug` is passed.
- `miracle-wifid` restricts P2P to 2.4 GHz (`P2P_SET disallow_freq 5180-5900`). Discovery uses the social channels 1, 6 and 11.
- `miracle-sinkctl run <link>` sets the Wi-Fi Display information element (`000600111c4400c8`: primary sink, session available, control port 7236) and issues `P2P_FIND`. It accepts incoming group owner negotiation requests automatically.
- The sink asks the source to send RTP to UDP port 7236 and plays it with `miracle-gst` (GStreamer elements `rtpmp2tdepay`, `tsdemux`, `h264parse`, `avdec_h264`). The player output goes to the journal under `miracle-sinkctl-gst`.
- `miracle-sinkctl` only prints peers that carry Wi-Fi Display information. A phone is only visible to the PC while the phone itself is scanning (Smart View device list open).

## Step 1: Hardware probe (read-only)

```bash
./scripts/miracast-probe.sh
```

Installs `iw`, `pciutils` and `usbutils` via `pacman` if they are missing (pass `--no-install` to skip), then reports P2P support. The output is redacted (MAC, IP, SSID). Without `P2P-GO` and `P2P-client` support a Wi-Fi Direct sink is unlikely on that adapter.

## Step 2: Preflight (read-only)

Install MiracleCast first (AUR): `yay -S miraclecast-git`. Then run:

```bash
./scripts/miracast-sink.sh --check
```

This changes nothing and Wi-Fi stays up. It prints the NetworkManager state of the adapter, running `wpa_supplicant` processes, the firewall state and any missing GStreamer elements.

## Step 3: Run the receiver

```bash
./scripts/miracast-sink.sh
```

**Wi-Fi on the adapter drops while this runs.** The script:

1. releases the adapter from NetworkManager and stops `wpa_supplicant.service`;
2. opens UDP 7236 and DHCP in `firewalld` for the duration of the run, if `firewalld` is active;
3. starts `miracle-wifid` with debug logging, sets the friendly name and runs `miracle-sinkctl`;
4. on exit (viewer closed, `quit`, error, terminal close) stops the daemons, removes the firewall rules, restarts `wpa_supplicant.service`, hands the adapter back to NetworkManager and waits for the connection.

None of these changes are persistent, so a reboot also restores the normal state. If Wi-Fi does not return, run `sudo systemctl restart NetworkManager`.

On the phone, open Smart View, keep the device list open for 60 seconds and look for `Arch-Miracast` (change it with `--name`). Then end the session by closing the viewer or typing `quit` in the terminal.

### Reading the report

The script prints a report on exit and saves it to `./miracast-sink-report.log` (MAC and IP redacted). The raw log is `./miracast-sink.log` and contains MAC addresses: do not publish it.

| Report field | Meaning |
| --- | --- |
| `stale P2P-device wdev=yes` | Another process still held the adapter's P2P device before `miracle-wifid` started. |
| `p2p_state=none` or `wifi_display=none` | The spawned `wpa_supplicant` did not bring up P2P or Wi-Fi Display. See the problem lines. |
| `P2P peers seen ... peak=0` | The PC received no P2P probe responses at all. With the phone scanning next to it, this points at the driver or firmware. |
| `peak>0` but the phone does not list the PC | Discovery works in one direction only. The PC's probe responses are not reaching the phone, or the phone filters the PC. |
| `go-neg-request>0`, `group-started=0` | The phone tried to connect and group formation failed. |
| `group-started>0`, no picture | Wi-Fi Direct works. Look at DHCP, the firewall and the player section of the raw log. |

## Step 4: Capture the negotiation (only if the phone connects)

Capture on the P2P interface with `tcpdump -i <P2P_INTERFACE> -w session.pcap` (RTSP on TCP 7236). Before sharing a capture or notes, replace MAC addresses, IP addresses, device serials and device names with placeholders (`<MAC>`, `<HOST_IP>`, `<DEVICE_SERIAL>`).

Questions the capture should answer:

- Which RTSP/WFD parameters does the phone request when the user chose Wireless DeX versus Smart View?
- Does the phone enable a desktop session, or only mirror?
- How is input from the PC returned (UIBC or another channel)?

## Full screen viewer and mouse/keyboard input (UIBC)

`miracast-sink.sh` runs `miracle-sinkctl --uibc` with a temporary configuration (a private `HOME` containing `.miraclecastrc`) that sets:

- `external-player` to `scripts/miracast-player.sh`, which drops root and starts `scripts/miracast-viewer.py` as the desktop user;
- `extends.wfd_uibc_capability` to `input_category_list=HIDC;generic_cap_list=none;hidc_cap_list=Keyboard/USB, Mouse/USB;port=none`.

MiracleCast's own UIBC client only implements the Generic category with touch down/up and key events, which has no pointer movement or right button. DeX needs a relative mouse with several buttons and a keyboard, so the viewer implements the HIDC category itself:

- UIBC header: version 0, no timestamp, input category 1 (HIDC), 16-bit total length.
- HIDC body: input path 1 (USB), HID type (0 keyboard, 1 mouse), usage (0 report, 1 report descriptor), 16-bit length, HID data.
- After connecting to the source's UIBC port, the viewer sends a report descriptor for a 5-button wheel mouse and for a boot-compatible keyboard, then input reports.
- The mouse is relative: the pointer is grabbed, hidden and re-centred after every motion event. Ctrl+Alt releases the grab.
- Keys are mapped from Linux evdev codes to HID usages using the kernel's `usbkbd.c` table.

The packet format was checked against a local test listener only. Whether Samsung's source accepts HIDC from this sink, and whether it needs the descriptors, is not yet known. If the phone answers the capability with `none`, the viewer runs video only and logs `UIBC not offered by the phone`. A Generic-category fallback (absolute pointer, no right button) is a possible next step.

Closing the viewer ends the session: the player wrapper sends SIGTERM to `miracle-sinkctl`, which exits, and the main script restores Wi-Fi. `Ctrl+C` is not a reliable way to stop, because `miracle-sinkctl` handles SIGINT by clearing its input line (`src/ctl/ctl-cli.c`, `cli_signal_fn`).

`scripts/install-launcher.sh` adds a desktop menu entry that runs the receiver in a terminal.

## DeX stage: open questions and leads

None of this is verified.

- **Receiver filtering.** It is unknown whether the Wireless DeX page lists every Miracast sink that Smart View lists, or only sinks with particular Wi-Fi Display attributes.
- **Mirror or desktop.** Whether the phone renders a DeX desktop for a generic sink is decided on the phone. It has not been observed with a Linux sink in this project.
- **Wi-Fi Display over an existing network.** `server/src/com/scrcpydex/server/DexActivator.java` starts a Wireless DeX session through Samsung's `SemWifiDisplayConfig` API with `setApConnection(<address>, <port>, <name>, <MAC>)` and `setMode(2)`. This is a Wi-Fi Display session addressed by IP rather than by Wi-Fi Direct. Today it targets `127.0.0.1` and the phone-side code plays the sink role. If the same call accepts the PC's LAN address, the PC could be the sink over the normal Wi-Fi network, with no Wi-Fi Direct and therefore no dependency on `ath10k` P2P support. It would still need `adb` to start the session, so it does not satisfy "tap the Wireless DeX button", but it is a fallback if Wi-Fi Direct cannot be made to work on this adapter.

## Legal note

Analysis must stay within the interoperability scope described in `LEGAL_AND_LICENSING.md`. Only capture traffic from your own devices.

## Outcomes to report

Share the preflight output and the report from Step 3, plus what the phone showed: whether Smart View listed the PC, whether tapping it connected, and whether a picture appeared.
