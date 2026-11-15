# sendspin-hdmi

Turn a small Linux box (or a Proxmox LXC) with an HDMI port into a **Music Assistant speaker with a now-playing screen**.

- Plays audio as a Sendspin player, so it stays in sync with your other Music Assistant speakers and can join their groups.
- Sends the sound over **HDMI**, e.g. to an AV receiver or TV.
- Shows a full-screen **now-playing page** on the same HDMI output: cover art, title, artist, album, time-synced lyrics, and Music Assistant's real waveform as the progress bar.
- Has a **settings page** for your phone or laptop: Music Assistant connection, audio output with a test tone, screen options, group membership, and amp control (Denon/Marantz, webhook or shell command when playback starts and stops).

> Unofficial community project. It is not made by or affiliated with the Open Home Foundation, Music Assistant or Sendspin.

## How it works

```
Music Assistant ──Sendspin (audio)──▶ sendspin client ──HDMI audio──▶ AV receiver / TV
       │                                                                    ▲
       └──websocket API (track, cover, lyrics, waveform)──▶ web app ──▶ Chromium kiosk ──HDMI video
```

| Service | What it does |
|---|---|
| `sendspin-hdmi-player` | The [sendspin](https://pypi.org/project/sendspin/) client playing to the HDMI audio device |
| `sendspin-hdmi-web` | Now-playing page and settings page (port 8080), Music Assistant client, auto-rejoin of groups |
| `sendspin-hdmi-kiosk` | sway + Chromium full screen on the HDMI output |
| `sendspin-hdmi-bus` | Private D-Bus for the player's metadata (fallback when Music Assistant isn't the source) |

Everything shown on the screen comes from Music Assistant, so it matches what Music Assistant shows: the same cover, the same lyrics (from MA's lyrics providers) and the same waveform analysis. Before MA has analysed a track the progress bar is a plain bar. Nothing on the screen is made up.

## Requirements

- Music Assistant with Sendspin support (tested with 2.11 beta) and its Sendspin player provider enabled.
- A Music Assistant **long-lived token**: in Music Assistant open your profile (top right) → *Long-lived access tokens* → *Create*.
- A machine with an HDMI output that Linux can drive: an Intel/AMD mini PC is ideal. Debian 12 or 13 (container, VM with GPU passthrough, or bare metal).

## Install on Proxmox (recommended)

This creates an unprivileged Debian container that uses the Proxmox host's own HDMI port for sound and picture. Run on the **Proxmox host** as root:

```bash
curl -fsSL https://raw.githubusercontent.com/slm020/sendspin-hdmi/main/proxmox/create-lxc.sh -o create-lxc.sh
bash create-lxc.sh -- --ma-url http://YOUR_MA_HOST:8095 --name "Living Room TV"
```

It asks for the token and a settings password (or generates one), then prints the settings page address. Options: `--ctid`, `--hostname`, `--storage`, `--bridge`, `--ip 192.168.1.60/24 --gw 192.168.1.1`, `--memory`, `--cores`, `--disk`, `--yes`. Run with `--help` for the full list.

What the script changes on the host, and what that means:

- It adds `/etc/udev/rules.d/90-sendspin-hdmi.rules`, which makes the host's sound (`/dev/snd`) and GPU (`/dev/dri`) devices mode `0666`. An unprivileged container needs this to use them. It also means any local user, and any other container you give these devices to, can use the host's sound cards and GPU. The script asks before writing it.
- The container drives the host's HDMI output directly, so the Proxmox text console on that screen is replaced by the now-playing screen while the container runs.
- Only one container can use the HDMI output at a time.

## Install on any Debian 12/13 machine

Run as root on the machine (or inside a container or VM that already has `/dev/snd` and `/dev/dri`):

```bash
curl -fsSL https://raw.githubusercontent.com/slm020/sendspin-hdmi/main/install.sh -o install.sh
bash install.sh --ma-url http://YOUR_MA_HOST:8095 --name "Living Room TV"
```

Use `--no-screen` for a headless audio-only player. Running the installer again upgrades the app and keeps your settings.

## Settings page

Open `http://<player-ip>:8080/settings` and log in with the password you set (any user name).

| Section | What you can set |
|---|---|
| Status | Connection state, the player's Music Assistant ID, service status, restart buttons |
| Music Assistant | Address, token (write-only: it is never shown again), connection test, optional Sendspin server URL |
| Player & audio | Player name, HDMI output (automatic picks the connected TV or receiver), rescan, **test tone**, sync delay, audio format |
| Screen | On/off, resolution, title, lyrics, waveform, clock (12/24 h), idle screen (logo + clock, logo, black), lyric timing offset, accent colour |
| Groups | Tick the Music Assistant sync groups this player is a saved member of; auto-rejoin after a restart |
| Amp control | On start / on stop: Denon/Marantz (power, input, sound mode over telnet), webhook (e.g. a Home Assistant webhook automation) or shell command; delay before the stop action |
| Security | Settings password, web port |

The now-playing page itself (`http://<player-ip>:8080/`) needs no password and works in any browser.

### Groups and auto-rejoin

Music Assistant adds a sync group's saved members when the group *starts* playing. If the player restarts partway through a song (update, reboot, settings change), Music Assistant leaves it out until the group starts again. sendspin-hdmi puts it back by itself: for a short window after the player reconnects (120 s by default), it rejoins a playing group that has it as a saved member. If you take the player out of a group later on, it stays out.

### Amp control

The start action runs when audio starts. The stop action runs after the configured delay (60 s by default), and is cancelled if playback resumes in the meantime, so pauses and track changes don't switch your amp off. Music Assistant announcements also count as playback.

Shell commands run as root and receive the `SENDSPIN_*` environment variables from the player.

## Troubleshooting

| Problem | Try |
|---|---|
| No sound, but Music Assistant says it's playing | Check the receiver is on the right input and its volume. Press *Play test tone*. On a fresh Sendspin client the software volume would start at 25 %; sendspin-hdmi sets it to 100 % on first start. |
| Wrong or missing HDMI output in the list | The player holds the device it is using. Press *Rescan* (stops audio briefly). HDMI outputs are only named after the connected device once the TV or receiver is on. |
| No picture, or picture but no sound at 4K | Use 1920x1080. On some Intel mini PCs, 4K modes gave no picture and also cut HDMI audio. |
| Screen stays black after the TV or receiver was off | On the host: `echo detect > /sys/class/drm/card0-HDMI-A-1/status` (use your connector name from `ls /sys/class/drm`), then restart the screen. |
| Player not found in Music Assistant | The status box shows the Sendspin server URL. It must be reachable (port 8927 on the MA host). Check the player service log: `journalctl -u sendspin-hdmi-player`. |
| Lyrics early or late | Adjust *Lyric timing*, or the *Sync delay* if the audio itself is out of sync with your other speakers. |

Logs: `journalctl -u sendspin-hdmi-player -u sendspin-hdmi-web -u sendspin-hdmi-kiosk -f`

## Files

| Path | What |
|---|---|
| `/etc/sendspin-hdmi/config.json` | All settings (mode 600; contains the MA token) |
| `/opt/sendspin-hdmi/` | App and Python environment |
| `/var/lib/sendspin-hdmi/sendspin/` | Sendspin identity (keep it to keep the same player in Music Assistant) |

To move an existing player to a new machine without Music Assistant seeing a new player, copy `/var/lib/sendspin-hdmi/sendspin/identity-daemon.json` across before starting it.

## Uninstall

```bash
systemctl disable --now sendspin-hdmi-kiosk sendspin-hdmi-web sendspin-hdmi-player sendspin-hdmi-bus
rm /etc/systemd/system/sendspin-hdmi-*.service
rm -r /opt/sendspin-hdmi /etc/sendspin-hdmi /var/lib/sendspin-hdmi
```

On a Proxmox host, also remove `/etc/udev/rules.d/90-sendspin-hdmi.rules` if no other container needs it.

## Licence

MIT. See [LICENSE](LICENSE).
