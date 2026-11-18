# Miracast Receiver: Beta Test Guide

This guide covers the `beta` branch: the laptop acts as a Miracast receiver for the phone's Wireless DeX (or Smart View), shows the picture in a full screen viewer, and sends the laptop's mouse and keyboard to the phone.

**Status.** Discovery and connection have worked once on the test laptop. The full screen viewer and the mouse/keyboard input are new and have only been tested on a development machine with a simulated phone. Whether the phone accepts the laptop's mouse and keyboard is not known yet.

## Before you start

- **Open this guide on the phone.** The laptop's Wi-Fi goes down while the receiver runs.
- **Know your sudo password.** The script asks for it once near the start. The script is not finished when the password prompt appears: type the password and press Enter.
- **Keep the phone close to the laptop**, with Wi-Fi switched on.
- **Plug in the laptop charger.**

Commands use long options (`--oneline`, `--check`) so that `l` and `1` cannot be confused.

## Part A: Get the latest beta version (Wi-Fi stays on)

**Step 1.** Go to the project folder. Change the path if your copy is somewhere else.

```bash
cd ~/Music/Dex4Arch
```

**Step 2.** If you are already on `beta`, download the latest version:

```bash
git pull
```

If you are not on `beta` yet, run these two instead:

```bash
git fetch origin beta
```

```bash
git switch beta
```

**Step 3.** Confirm the version. The top line should end in `reduce input and video latency, harden full screen`.

```bash
git log --oneline --max-count=3
```

## Part B: Preflight (read-only, Wi-Fi stays on)

**Step 4.** Run the check. It changes nothing.

```bash
./scripts/miracast-sink.sh --check
```

**Step 5.** Look at the line starting with `viewer:`.

- `ok, video sinks: xvimagesink, ...` means the viewer can run.
- Anything else means a package is missing. Install the viewer's dependencies, then run step 4 again:

```bash
sudo pacman -S --needed python-gobject gtk3 gst-plugins-base gst-libav libnotify
```

**Step 5b.** Look at the line starting with `H.264 decoders:`. Decoding the phone's video in software is the most likely cause of delay on a low-power laptop, because frames pile up when the CPU cannot keep up.

- `(hardware decoding available)`: nothing to do.
- `(software only ...)`: install hardware video decoding for Intel graphics, then run step 4 again:

```bash
sudo pacman -S --needed gst-plugin-va libva-intel-driver libva-utils
```

Then check that the graphics driver offers H.264 decoding. The output should contain lines with `H264`:

```bash
vainfo
```

If `pacman` reports `target not found`, or `vainfo` shows an error, photograph it and send it.

Also look at the line starting with `resolution offered:`. It shows the laptop screen size the script detected and the mode the phone is asked to use, for example `preferred VESA 1366x768p60`.

## Part C: Run the receiver (Wi-Fi goes down)

**Step 6.** Start the receiver. **The laptop's Wi-Fi drops now** and comes back when the session ends.

```bash
./scripts/miracast-sink.sh
```

Enter your sudo password when asked. Wait for `Receiver "Arch-Miracast" is ready.`

**Step 7.** On the phone: **Settings > Connected devices > Samsung DeX > Connect wirelessly**, then tap `Arch-Miracast`.

**Step 8.** A full screen window with the phone's picture opens on the laptop. Note whether it shows the DeX desktop or a copy of the phone screen.

**Step 9.** Click once inside the picture. The laptop's cursor disappears, and your mouse and keyboard now control the phone. Try:

- moving the mouse, and left- and right-clicking;
- scrolling with the touchpad or mouse wheel;
- typing in a text field;
- the Windows (Super) key, which opens the DeX app list.

**Step 10.** Press **Ctrl+Alt** to give the mouse and keyboard back to the laptop. While they are released:

| Key | Action |
| --- | --- |
| Click the picture | Control the phone again |
| F11 | Leave or enter full screen |
| Ctrl+Q | Close the viewer and end the session |

### Resolution

By default the script offers the phone only the resolutions that fit the laptop screen, and marks the screen's own size as preferred. Options if the result is not right:

| Command | Effect |
| --- | --- |
| `./scripts/miracast-sink.sh --max-fps 30` | Offer 30 fps modes only. Lighter on the laptop's CPU if the picture stutters. |
| `./scripts/miracast-sink.sh --res 1280x720` | Offer modes up to 1280x720. Try this if the phone refuses the screen's own size. |
| `./scripts/miracast-sink.sh --res all` | Offer everything up to 1920x1080, as before. |

The resolution the phone actually chose appears in the report, on the `viewer: start:` line.

### Latency and input

- The viewer uses the hardware decoder when one is installed (step 5b) and falls back to software decoding automatically.
- The report's `viewer performance` lines show frames decoded per second and the `decode backlog`. A backlog that stays above about 50 ms means the laptop cannot decode fast enough. Try `--max-fps 30`, or a lower `--res`.
- If the pointer moves too slowly or too fast on the phone, change the multiplier, for example `--mouse-speed 1.5`. The phone's own pointer speed setting also applies.
- Some delay comes from the phone's video encoder and the Wi-Fi link, and the laptop cannot remove that part.

### Full screen

- The viewer opens full screen and asks to stay above the laptop's panel. If the laptop's taskbar still shows, press Ctrl+Alt, then F11 twice.
- The bar at the bottom of the DeX desktop is the phone's DeX taskbar, not the laptop's. On the phone, **Settings > Connected devices > Samsung DeX > Auto hide taskbar** hides it.
- `--windowed` starts the viewer in a normal window.

## Part D: End the session

**Step 11.** End the session in one of these ways:

- press **Ctrl+Alt**, then **Ctrl+Q** in the viewer; or
- type `quit` in the terminal and press Enter.

Do not rely on `Ctrl+C` in the terminal. According to the MiracleCast source, `miracle-sinkctl` treats it as "clear the input line" and keeps running.

**Step 12.** Wait for the `MIRACAST SINK REPORT` block in the terminal and photograph all of it. Look at the `viewer:` lines near the bottom. They show whether the phone opened the input channel (`UIBC connected` or `did not enable input`).

**Step 13.** Wi-Fi should reconnect by itself. If it has not after a minute:

```bash
sudo systemctl restart NetworkManager
```

## Part E: Add it to the start menu (optional)

**Step 14.** Install the menu entry. This only writes one file in your home folder.

```bash
./scripts/install-launcher.sh
```

Open the application menu and search for **Miracast**. The entry opens a terminal for the sudo password and the report. After the session it waits for Enter before the terminal closes.

To remove the entry:

```bash
./scripts/install-launcher.sh --remove
```

If you move the project folder, run the install command again.

## What to send back

1. Whether the laptop showed the **DeX desktop** or only **mirrored** the phone screen.
2. Whether the mouse moved the pointer on the phone, and whether clicking, right-clicking, scrolling and typing worked.
3. A photo of the report, especially the `viewer:` lines.
4. Whether the picture was smooth or laggy, and whether it filled the screen sharply.
5. The `resolution offered:`, `H.264 decoders:` and `viewer performance` lines.
6. Whether the laptop's taskbar was still visible over the picture.

Do not send `miracast-sink.log`. It contains real MAC addresses. Only the report has them removed.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| The viewer says the phone did not enable input | The phone refused the laptop's mouse/keyboard offer. Send the report; another input mode can be tried. |
| Clicking the picture shows `Input channel to the phone is not connected` | The input connection failed or is still connecting. Wait a few seconds and click again. If it persists, send the report. |
| The mouse is stuck inside the viewer | Press Ctrl+Alt. If that does not help, type `quit` in the terminal (Alt+Tab to reach it). |
| No window appears but the phone says connected | Send the report. The viewer messages explain why. To test without the viewer, run `./scripts/miracast-sink.sh --no-input`. |
| No sound | Sound plays by default. Use `--no-audio` to turn it off if it causes problems. |
| You closed the terminal by mistake | Run the NetworkManager restart in step 13. |

## Going back to the main version

```bash
git switch main
```
