# sendspin-hdmi

Turn a small Linux box, or a Proxmox container, into a Music Assistant speaker with a now-playing screen on your TV.

*Unofficial community project. Not made by or linked to the Open Home Foundation, Music Assistant or Sendspin.*

## What it does

- Plays music from Music Assistant as a Sendspin player, in sync with your other speakers.
- Sends the sound out over HDMI to your TV or AV receiver.
- Shows a full-screen now-playing page on the same HDMI output:
  - cover art, title, artist and album
  - lyrics that scroll in time with the song
  - Music Assistant's real waveform as the progress bar
  - a clock when nothing is playing
- Has a settings page you can open on your phone or laptop.
- Can switch your amp on and off when music starts and stops (optional).

Everything on the screen comes from Music Assistant, so it matches what Music Assistant shows. Nothing is made up: until Music Assistant has analysed a song, the progress bar is a plain bar.

## What you need

- Music Assistant with Sendspin (tested with version 2.11 beta).
- A Music Assistant **long-lived token**:
  - open Music Assistant
  - go to your user profile
  - create a long-lived token in its tokens section and copy it
- A computer with an HDMI port plugged into your TV or AV receiver. An Intel mini PC running Proxmox is ideal.
- Tested on Debian 13 in a Proxmox container. Other Debian set-ups should work but haven't been tested.

## Install on Proxmox (easiest)

- Log in to your Proxmox host as root (the machine whose HDMI port goes to your TV or amp).
- Download the script:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/slm020/sendspin-hdmi/main/proxmox/create-lxc.sh -o create-lxc.sh
  ```

- Run it, using your own Music Assistant address and a name for the player:

  ```bash
  bash create-lxc.sh -- --ma-url http://192.168.1.10:8095 --name "Living Room TV"
  ```

- Paste your token when asked.
- Pick a settings password, or press Enter to have one made for you.
- At the end it shows the settings page address. Open it and press **Play test tone**.

Good to know:

- It makes a new container and installs everything inside it.
- It lets the container use the host's sound and graphics. To do that it makes the host's sound and graphics devices usable by everyone on the host. It asks before doing this.
- While the container is running, the screen shows the now-playing page instead of the Proxmox console.
- Only one container can use the HDMI port at a time.
- Extra options: `--ctid`, `--hostname`, `--storage`, `--bridge`, `--ip 192.168.1.60/24 --gw 192.168.1.1`, `--memory`, `--cores`, `--disk`, `--yes`. Run with `--help` to see them all.

## Install on any other Debian machine

- Log in as root on the machine (it needs `/dev/snd` for sound and `/dev/dri` for the screen).
- Run:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/slm020/sendspin-hdmi/main/install.sh -o install.sh
  bash install.sh --ma-url http://192.168.1.10:8095 --name "Living Room TV"
  ```

- Add `--no-screen` if you only want sound.
- To update later, just run the installer again. Your settings are kept.

## Settings page

- Open `http://<player-address>:8080/settings`.
- Log in with your password. Any user name works.
- **Status**: whether it's connected, plus buttons to restart the player or screen.
- **Music Assistant**: address, token and a **Test connection** button.
- **Player & audio**: player name, HDMI output (*Automatic* picks whatever is plugged in), **Play test tone**, sync delay.
- **Screen**: turn the screen on or off, resolution, title, lyrics, waveform, clock, what to show when idle, lyric timing and accent colour.
- **Groups**: tick the Music Assistant groups this player should be part of.
- **Amp control**: what to do when music starts and stops:
  - Denon or Marantz receiver: switch it on, pick the input and sound mode, standby when stopped
  - Webhook, e.g. a Home Assistant automation
  - Shell command
- **Security**: change the password or the web port.

The now-playing page itself is at `http://<player-address>:8080/` and works in any browser, no password needed.

## Good to know

- **Groups:** like any Music Assistant player, it joins its saved groups when the group starts playing. If the player restarts in the middle of a song, it rejoins the next time the group starts.
- **Amp control:** the stop action waits 60 seconds (you can change this), so pauses and track changes don't switch your amp off. Music Assistant announcements count as music too.

## If something isn't working

- **No sound:**
  - check your amp is on the right input and the volume is up
  - press **Play test tone** on the settings page
- **The HDMI output isn't listed:** press **Rescan** (the sound stops for a moment). Outputs only show your TV's or amp's name once it's switched on.
- **No picture, or picture but no sound, at 4K:** use 1920x1080. Some Intel mini PCs lose the picture and the sound at 4K.
- **Blank screen after the TV or amp was off:**
  - on the host, run `echo detect > /sys/class/drm/card0-HDMI-A-1/status` (find your connector with `ls /sys/class/drm`)
  - then press **Restart screen**
- **The player doesn't show up in Music Assistant:** port 8927 on the Music Assistant machine must be reachable from the player.
- **Lyrics early or late:** change **Lyric timing**. If the sound itself is out of sync with your other speakers, change **Sync delay**.
- **Logs:** `journalctl -u sendspin-hdmi-player -u sendspin-hdmi-web -u sendspin-hdmi-kiosk -f`

## Where things are kept

- Settings: `/etc/sendspin-hdmi/config.json` (contains your token, readable by root only).
- App: `/opt/sendspin-hdmi/`
- Player identity: `/var/lib/sendspin-hdmi/sendspin/`. Copy `identity-daemon.json` to a new machine to keep the same player in Music Assistant.

## Uninstall

```bash
systemctl disable --now sendspin-hdmi-kiosk sendspin-hdmi-web sendspin-hdmi-player sendspin-hdmi-bus
rm /etc/systemd/system/sendspin-hdmi-*.service
rm -r /opt/sendspin-hdmi /etc/sendspin-hdmi /var/lib/sendspin-hdmi
```

- On Proxmox, also delete `/etc/udev/rules.d/90-sendspin-hdmi.rules` on the host if nothing else needs it.

## How it fits together

| Service | Job |
|---|---|
| `sendspin-hdmi-player` | Plays the music to the HDMI sound output |
| `sendspin-hdmi-web` | Now-playing page, settings page and the Music Assistant connection |
| `sendspin-hdmi-kiosk` | Shows the now-playing page full screen on HDMI |
| `sendspin-hdmi-bus` | Passes song details from the player to the web service as a backup |

## Licence

MIT. See [LICENSE](LICENSE).
