# Archipelago Live Item Feed

A lightweight, self-hosted live item tracker for [Archipelago](https://archipelago.gg) multiworld games. It connects to your Archipelago room as a tracker slot and streams every item check into a clean, web-based feed you can watch in a browser tab or drop straight into OBS as a stream overlay.

```
CTR sent Crystal to Crash 2 (Slide Coliseum: Item Box 12)
Crash 2 found their Wumpa (N. Sanity Beach: Box 4)
```

Each player gets their own consistent color, and items are color-coded by classification (trap / progression / useful / filler), so you can tell what's happening in the multiworld at a glance.

<p align="center">
  <img src="Pics/pic2.png" width="480" alt="Live item feed showing colored player names and items">
  <img src="Pics/pic1.png" width="480" alt="OBS Mode overlay with transparent background">
</p>

## Features

- **Live feed** — item sends and finds appear instantly as they happen in the room.
- **Per-player colors** — every player is automatically assigned a distinct color that's generated to never clash with the fixed location or item-classification colors.
- **Item classification colors** — trap (red), progression (purple), useful (blue), filler (default).
- **Multi-game aware** — correctly resolves item and location names even when different players in the room are playing different games with overlapping IDs.
- **Public and local rooms** — connect to a local server (`127.0.0.1`) or a public Archipelago lobby, with an optional room password.
- **OBS-ready** — a built-in OBS Mode strips the page down to a transparent background with no header or controls, so you can add it directly as a Browser Source overlay.
- **Save / Load checks** — export the current feed to a JSON file and reload it later to pick up where you left off.
- **Pause / Clear** — pause the feed without disconnecting, or clear it and start fresh.

## Requirements

- Python 3.9 or later
- The [`websockets`](https://pypi.org/project/websockets/) library

Install the dependency:

```bash
pip install websockets
```

## Usage

1. Run the tracker:

   ```bash
   python tracker.py
   ```

2. You'll be prompted for:
   - **Server address** — leave blank to connect to a local server at `127.0.0.1:38281`, or enter a public lobby address such as `archipelago.gg:38281`. A bare `host:port` is fine; `ws://` / `wss://` prefixes are also accepted.
   - **Room password** — leave blank if the room doesn't have one.
   - **Slot name** — the *exact* player slot name to track (case-sensitive).

3. Your browser will open automatically to `http://127.0.0.1:8080`, showing the live feed. If it doesn't, open that address manually.

The tracker keeps trying to reconnect if the connection to Archipelago drops, and the page will do the same for its connection to the tracker.

## Using it in OBS

1. Start the tracker and confirm the feed page loads normally in a browser.
2. In OBS, add a **Browser Source**.
3. Set the URL to:

   ```
   http://127.0.0.1:8080/index.html?obs=1
   ```

   (or open the page normally and click the **OBS Mode** button)

4. The page background, header, and controls will disappear, leaving just the event feed on a transparent background that composites cleanly over your other sources.

## Saving and loading checks

- **Save Checks** downloads the current feed (up to the last 150 events) as a timestamped `.json` file.
- **Load Checks** lets you pick that file back up and replays it into the feed — useful for resuming a session or reviewing checks later. This is entirely local to your browser; it doesn't touch the tracker process.

## Configuration

A few constants near the top of `tracker.py` can be changed if needed:

| Setting | Default | Purpose |
|---|---|---|
| `HTTP_HOST` / `HTTP_PORT` | `127.0.0.1:8080` | Where the feed webpage is served |
| `BROWSER_WS_HOST` / `BROWSER_WS_PORT` | `127.0.0.1:8765` | The local websocket the page uses to receive live events |

The Archipelago server address and room password are entered interactively when you run the script rather than hardcoded.

## Troubleshooting

- **`ConnectionRefused` / `InvalidSlot`** — the slot name doesn't exactly match a slot in the room. Slot names are case-sensitive.
- **Feed stays on "Waiting for item events..."** — make sure the tracker's console shows `Tracker connected successfully!`; if it's stuck reconnecting, double-check the server address and password.
- **Page shows "Disconnected — retrying..."** — the tracker process isn't running, or the browser can't reach `BROWSER_WS_PORT`. Make sure no firewall is blocking local connections.

## License

Licensed under the [MIT License](LICENSE) — you're free to use, modify, and redistribute this project, provided the original copyright notice is retained.
