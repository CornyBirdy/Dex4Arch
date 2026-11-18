# Miracast Receiver: Beta Test Guide

This guide covers one test run of `scripts/miracast-sink.sh` from the `beta` branch. The goal of this run is to find out whether the phone's Smart View can discover the laptop. DeX is not part of this test.

**Status: untested on real hardware.** The script has only been run against stub programs. Expect problems, and report what you see.

## Before you start

- **Open this guide on the phone.** The laptop's Wi-Fi goes down during step 9, so the laptop cannot show this page then.
- **Know your sudo password.** The script asks for it once near the start.
- **Keep the phone within 1 m of the laptop**, with Wi-Fi switched on. The phone does not need to be on the same network.
- **Plug in the laptop charger.** Power saving can put the Wi-Fi adapter to sleep.
- **Have the camera ready.** Steps 8 and 14 each need a photo of the terminal.

Commands are written with long options (`--oneline`, `--check`) so that `l` and `1` cannot be confused. None of the commands in this guide contain the digit `1`.

## Part A: Get the beta version (Wi-Fi stays on)

**Step 1.** Open a terminal and go to the project folder. Change the path if your copy is somewhere else.

```bash
cd ~/ScrcpyDex4Arch
```

**Step 2.** Check that there are no local changes that would block switching branches.

```bash
git status
```

If it lists changed files you want to keep, stop and report them. Files ending in `.log` are ignored by git and do not matter.

**Step 3.** Download the `beta` branch.

```bash
git fetch origin beta
```

**Step 4.** Switch to it.

```bash
git switch beta
```

If git says `invalid reference: beta`, use this instead:

```bash
git switch --track origin/beta
```

**Step 5.** Confirm you are on the right version.

```bash
git log --oneline --max-count=3
```

The list should include a line ending in `rework Miracast sink script with diagnostics and report`.

**Step 6.** Make sure the script can be executed. This is harmless if it already can.

```bash
chmod +x scripts/miracast-sink.sh
```

**Already on `beta` from an earlier test?** Skip steps 3 to 6 and download the latest version instead:

```bash
git pull
```

## Part B: Preflight (read-only, Wi-Fi stays on)

**Step 7.** Run the check. It changes nothing.

```bash
./scripts/miracast-sink.sh --check
```

**Step 8.** Photograph the whole block, from `MIRACAST PREFLIGHT` down to the closing line of `=` signs.

Look at the line `missing GStreamer elements:`. If it lists anything other than `none`, the picture will not be shown later even if discovery works. Run step 9 anyway, because discovery is what this test is for, and report the missing names.

## Part C: The receiver run (Wi-Fi goes down)

**Step 9.** Start the receiver. **The laptop's Wi-Fi and internet go down now** and come back when the script ends.

```bash
./scripts/miracast-sink.sh
```

Enter your sudo password when asked.

**Step 10.** Wait until the terminal shows:

```
[miracast] Receiver "Arch-Miracast" is starting.
```

This normally takes 5 to 20 seconds. If the script stops with `[miracast][ERROR]` instead, photograph the screen and go to step 15.

**Step 11.** On the phone, swipe down twice to open the quick panel and tap **Smart View**. Keep the device list open for a full 60 seconds. Do not tap anything else during that time.

Then close Smart View, open **Settings > Connected devices > Samsung DeX**, tap **Connect wirelessly** and keep that list open for another 60 seconds. Both pages search for Miracast receivers; checking both shows whether the DeX page filters receivers.

While you wait, watch the laptop terminal. A line containing `[ADD] Peer` means the laptop can see the phone.

**Step 12.** Note what the phone shows:

- **`Arch-Miracast` appears in the list.** Tap it, then wait 30 seconds. Note any message on the phone, such as "Connecting", "Couldn't connect" or a PIN prompt. If a PIN appears, photograph it and do not type anything.
- **`Arch-Miracast` does not appear after 60 seconds.** That is a useful result. Continue to step 13.

**Step 13.** On the laptop, press `Ctrl+C` **once**. Do not close the terminal window.

**Step 14.** Wait up to 30 seconds. A block headed `MIRACAST SINK REPORT` appears. Photograph all of it. If it does not fit on screen, take two photos, or scroll up and photograph the top half too.

The report is also saved as `miracast-sink-report.log` in the project folder, with MAC and IP addresses removed. The script prints the exact path near the start of the run (`Report will be saved to:`). If you cannot find it later, search for it:

```bash
find ~ -name "miracast-sink*"
```

## Part D: Check the laptop is back to normal

**Step 15.** Check that Wi-Fi has reconnected. The last line of the report shows the state, and `100 (connected)` means it is back. You can also check with:

```bash
nmcli device status
```

**Step 16.** If Wi-Fi is not back after a minute, restart NetworkManager:

```bash
sudo systemctl restart NetworkManager
```

If that does not help, reboot. The script makes no permanent changes, so a reboot restores everything.

## What to send back

1. The photo of the preflight output (step 8).
2. The photo or photos of the report (step 14).
3. Whether `[ADD] Peer` lines appeared on the laptop (step 11).
4. Whether Smart View listed `Arch-Miracast`, and what happened if you tapped it (step 12).
5. Whether Wi-Fi came back on its own (step 15).

Do not send `miracast-sink.log`. It contains real MAC addresses. Only the report has them removed.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `'miracle-wifid' not found` or a similar missing-program message | Photograph it and report it. Do not install anything yet. |
| `miracle-wifid did not list wlan0 as a link` | Photograph the screen. Wi-Fi is restored automatically. |
| `wpa_supplicant did not report a P2P state within 15 s` | Not fatal. Continue with step 11 so the report can show why. |
| A video window opens but stays black | Wait 30 seconds, then continue with step 13. Report that a window appeared. |
| The terminal stops responding after `Ctrl+C` | Wait a full minute, then press `Ctrl+C` once more. |
| You closed the terminal by mistake | Run step 16. |

## Going back to the main version

```bash
git switch main
```
