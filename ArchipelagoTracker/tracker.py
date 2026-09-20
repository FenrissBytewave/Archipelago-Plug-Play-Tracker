import asyncio
import colorsys
import json
import threading
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets


# ============================================================
# SETTINGS
# ============================================================

AP_HOST = "127.0.0.1"
AP_PORT = 38281

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8080

BROWSER_WS_HOST = "127.0.0.1"
BROWSER_WS_PORT = 8765

# Room password is now asked for interactively at startup instead
# of being hardcoded here (public lobbies almost always need one).

# Archipelago protocol version.
PROTOCOL_VERSION = {
    "major": 0,
    "minor": 6,
    "build": 6,

    # Required: Archipelago's JSON decoder uses this key to rebuild
    # the value as a Version object. Without it the server compares
    # Version > dict and the connection is dropped.
    "class": "Version"
}


# ============================================================
# SERVER ADDRESS PARSING
# ============================================================

def parse_server_address(raw_address):
    """
    Accepts whatever a person is likely to paste for a public
    Archipelago lobby or a local server, and returns a proper
    websocket URI.

    Handles:
        127.0.0.1:38281
        archipelago.gg:38281
        ws://archipelago.gg:38281
        wss://archipelago.gg:38281
    """

    address = raw_address.strip()

    if not address:
        return f"ws://{AP_HOST}:{AP_PORT}"

    # Already a full websocket URI.
    if (
        address.startswith("ws://")
        or
        address.startswith("wss://")
    ):
        return address

    # Public Archipelago rooms (e.g. archipelago.gg) are served
    # over TLS, so default to a secure socket for anything that
    # isn't a plain loopback/LAN address without a scheme.
    if (
        address.startswith("127.0.0.1")
        or
        address.startswith("localhost")
        or
        address.startswith("0.0.0.0")
    ):
        return f"ws://{address}"

    return f"wss://{address}"


# ============================================================
# GLOBAL DATA
# ============================================================

browser_clients = set()

# Item/location IDs are only unique WITHIN a game, not across games in
# the multiworld, so names must be looked up per-game, not from one
# flattened table.
item_names_by_game = {}
location_names_by_game = {}

# Kept as a fallback for games we can't identify a slot's game for.
item_names = {}
location_names = {}

player_games = {}

player_names = {}
player_colors = {}

seen_events = set()


# ============================================================
# PLAYER COLORS
# ============================================================
#
# Player colors are generated on the fly (rather than picked from
# a small fixed list) for two reasons:
#
#   1. It scales to any number of players without ever running out
#      and falling back to a random, possibly duplicate, color.
#
#   2. Each generated hue is checked against the fixed colors used
#      for locations and item classifications (below) and nudged
#      away from them, so a player can never end up looking the
#      same color as a location name or an item type.
#
# Hues are spaced using the golden angle, which is the standard
# trick for picking N points around a circle that stay maximally
# spread out from each other no matter how many you generate.

GOLDEN_ANGLE_DEGREES = 137.508

PLAYER_SATURATION = 0.68
PLAYER_LIGHTNESS = 0.72

# Approximate hue (in degrees, 0-360) of each fixed color below:
#   location   #fbbf24  (amber)   ~ 38
#   trap       #ef4444  (red)     ~  0
#   useful     #60a5fa  (blue)    ~ 217
#   progression #c084fc (purple)  ~ 272
# Filler (#e5e7eb) is near-gray/no real hue, and player colors are
# always fully saturated, so it can't be confused with a player
# color and doesn't need an entry here.
RESERVED_HUES = [0, 38, 217, 272]
HUE_EXCLUSION_DEGREES = 18


def _hue_distance(hue_a, hue_b):

    diff = abs(hue_a - hue_b) % 360

    return min(
        diff,
        360 - diff
    )


def _hue_is_reserved(hue_degrees):

    return any(
        _hue_distance(hue_degrees, reserved)
        < HUE_EXCLUSION_DEGREES
        for reserved in RESERVED_HUES
    )


def _hsl_to_hex(hue_degrees, saturation, lightness):

    red, green, blue = colorsys.hls_to_rgb(
        (hue_degrees % 360) / 360.0,
        lightness,
        saturation,
    )

    return "#{:02x}{:02x}{:02x}".format(
        round(red * 255),
        round(green * 255),
        round(blue * 255),
    )


def _generate_player_color(index):

    hue = (
        index
        *
        GOLDEN_ANGLE_DEGREES
    ) % 360

    # Nudge away from any reserved (location/item) hue.
    nudges = 0

    while (
        _hue_is_reserved(hue)
        and
        nudges < 360
    ):

        hue = (hue + 1) % 360

        nudges += 1

    return _hsl_to_hex(
        hue,
        PLAYER_SATURATION,
        PLAYER_LIGHTNESS
    )


def get_player_color(player_name):

    if player_name not in player_colors:

        index = len(player_colors)

        color = _generate_player_color(
            index
        )

        # Belt-and-braces: the golden-angle spacing makes an exact
        # repeat vanishingly unlikely, but guard against it anyway
        # so two players can never end up sharing a color.
        guard = 0

        while (
            color in player_colors.values()
            and
            guard < 100
        ):

            index += 1

            color = _generate_player_color(
                index
            )

            guard += 1

        player_colors[player_name] = color

    return player_colors[player_name]


# ============================================================
# ITEM CLASSIFICATION
# ============================================================

def get_item_classification(flags):

    try:
        flags = int(flags)
    except (TypeError, ValueError):
        flags = 0

    # Archipelago:
    # 1 = Progression
    # 2 = Useful
    # 4 = Trap

    if flags & 4:
        return "trap"

    if flags & 1:
        return "progression"

    if flags & 2:
        return "useful"

    return "filler"


# ============================================================
# NAME LOOKUPS
# ============================================================

def get_item_name(item_id, game=None):

    if game is not None:

        table = item_names_by_game.get(
            game,
            {}
        )

        if str(item_id) in table:
            return table[str(item_id)]

    return item_names.get(
        str(item_id),
        f"Unknown Item ({item_id})"
    )


def get_location_name(location_id, game=None):

    if game is not None:

        table = location_names_by_game.get(
            game,
            {}
        )

        if str(location_id) in table:
            return table[str(location_id)]

    return location_names.get(
        str(location_id),
        f"Unknown Location ({location_id})"
    )


def get_player_name(slot):

    return player_names.get(
        str(slot),
        f"Player {slot}"
    )


# ============================================================
# DATA PACKAGE
# ============================================================

def load_datapackage(data):

    if not isinstance(data, dict):
        return

    games = data.get(
        "games",
        {}
    )

    if not isinstance(games, dict):
        return

    for game_name, game_data in games.items():

        if not isinstance(game_data, dict):
            continue

        game_items = item_names_by_game.setdefault(
            game_name,
            {}
        )

        game_locations = location_names_by_game.setdefault(
            game_name,
            {}
        )

        # ----------------------------------------
        # Items
        # ----------------------------------------

        item_table = game_data.get(
            "item_name_to_id",
            {}
        )

        if isinstance(item_table, dict):

            for name, item_id in item_table.items():

                game_items[
                    str(item_id)
                ] = name

                # Fallback table, in case we can't determine
                # which game an item/location belongs to.
                item_names[
                    str(item_id)
                ] = name

        # ----------------------------------------
        # Locations
        # ----------------------------------------

        location_table = game_data.get(
            "location_name_to_id",
            {}
        )

        if isinstance(location_table, dict):

            for name, location_id in location_table.items():

                game_locations[
                    str(location_id)
                ] = name

                location_names[
                    str(location_id)
                ] = name

    print(
        f"Loaded {len(item_names)} item names "
        f"and {len(location_names)} location names."
    )


# ============================================================
# PLAYER LIST
# ============================================================

def update_players(players):

    if not isinstance(players, list):
        return

    for player in players:

        if not isinstance(player, dict):
            continue

        slot = player.get(
            "slot"
        )

        if slot is None:
            continue

        name = player.get(
            "alias"
        )

        if not name:
            name = player.get(
                "name",
                f"Player {slot}"
            )

        player_names[
            str(slot)
        ] = name

        get_player_color(
            name
        )


# ============================================================
# SLOT -> GAME MAP
# ============================================================

def update_slot_games(slot_info):

    if not isinstance(slot_info, dict):
        return

    for slot, info in slot_info.items():

        if not isinstance(info, dict):
            continue

        game = info.get(
            "game"
        )

        if not game:
            continue

        player_games[
            str(slot)
        ] = game


# ============================================================
# BROWSER BROADCAST
# ============================================================

async def broadcast_event(event):

    if not browser_clients:
        return

    message = json.dumps(
        event
    )

    dead_clients = set()

    for client in list(browser_clients):

        try:

            await client.send(
                message
            )

        except Exception:

            dead_clients.add(
                client
            )

    for client in dead_clients:

        browser_clients.discard(
            client
        )


# ============================================================
# BROWSER WEBSOCKET
# ============================================================

async def browser_websocket(
    websocket
):

    browser_clients.add(
        websocket
    )

    print(
        "Browser connected."
    )

    try:

        await websocket.wait_closed()

    finally:

        browser_clients.discard(
            websocket
        )

        print(
            "Browser disconnected."
        )


# ============================================================
# ITEM EVENT
# ============================================================

async def process_item_send(
    packet
):

    item = packet.get(
        "item",
        {}
    )

    if not isinstance(
        item,
        dict
    ):
        return

    item_id = item.get(
        "item"
    )

    location_id = item.get(
        "location"
    )

    source_slot = item.get(
        "player"
    )

    destination_slot = packet.get(
        "receiving"
    )

    flags = item.get(
        "flags",
        0
    )

    if item_id is None:
        return

    if source_slot is None:
        return

    if destination_slot is None:
        return

    # ----------------------------------------
    # Names
    #
    # Item/location IDs are only unique within a game, so each must
    # be resolved against the right game's table:
    #   - the LOCATION belongs to the SENDING player's world (that's
    #     whose world the check physically happened in)
    #   - the ITEM belongs to the itempool of the RECEIVING player's
    #     game (that's the world it was generated for, even though
    #     it got shuffled into someone else's location)
    # ----------------------------------------

    source_game = player_games.get(
        str(source_slot)
    )

    destination_game = player_games.get(
        str(destination_slot)
    )

    item_name = get_item_name(
        item_id,
        destination_game
    )

    location_name = get_location_name(
        location_id,
        source_game
    )

    source_name = get_player_name(
        source_slot
    )

    destination_name = get_player_name(
        destination_slot
    )

    # ----------------------------------------
    # Classification
    # ----------------------------------------

    classification = get_item_classification(
        flags
    )

    # ----------------------------------------
    # Colors
    # ----------------------------------------

    source_color = get_player_color(
        source_name
    )

    destination_color = get_player_color(
        destination_name
    )

    # ----------------------------------------
    # Duplicate protection
    # ----------------------------------------

    event_key = (
        source_slot,
        destination_slot,
        item_id,
        location_id,
    )

    if event_key in seen_events:
        return

    seen_events.add(
        event_key
    )

    # ----------------------------------------
    # Own item
    # ----------------------------------------

    if source_slot == destination_slot:

        event_type = "found"

        sentence = (
            f'"{source_name}" found their '
            f'"{item_name}"'
        )

    # ----------------------------------------
    # Sent to another player
    # ----------------------------------------

    else:

        event_type = "sent"

        sentence = (
            f'"{source_name}" sent '
            f'"{item_name}" to '
            f'"{destination_name}"'
        )

    # ----------------------------------------
    # Console
    # ----------------------------------------

    print(
        sentence,
        f'("{location_name}")'
    )

    # ----------------------------------------
    # Browser event
    # ----------------------------------------

    await broadcast_event({

        "type": "item_event",

        # The page reads data.event, so the payload is nested here.
        "event": {

            "event_type": event_type,

            "source": source_name,
            "source_slot": source_slot,
            "source_color": source_color,

            "destination": destination_name,
            "destination_slot": destination_slot,
            "destination_color": destination_color,

            "item": item_name,

            "location": location_name,

            "classification": classification,

            "flags": flags,

        },

    })


# ============================================================
# ARCHIPELAGO CONNECTION
# ============================================================

async def connect_to_archipelago(
    slot_name,
    server_uri,
    password
):

    while True:

        try:

            print()
            print(
                "=========================================="
            )

            print(
                "Connecting to Archipelago..."
            )

            print(
                f"Server: {server_uri}"
            )

            print(
                f"Tracker slot: {slot_name}"
            )

            print(
                "=========================================="
            )

            async with websockets.connect(
                server_uri,
                max_size=16 * 1024 * 1024,
            ) as websocket:

                print(
                    "Connected to Archipelago."
                )

                # ====================================================
                # WAIT FOR ROOM INFO
                # ====================================================

                async for raw_message in websocket:

                    try:

                        packets = json.loads(
                            raw_message
                        )

                    except json.JSONDecodeError:

                        print(
                            "Received invalid JSON:"
                        )

                        print(
                            raw_message
                        )

                        continue

                    # Archipelago packets are lists.
                    if isinstance(
                        packets,
                        dict
                    ):

                        packets = [
                            packets
                        ]

                    if not isinstance(
                        packets,
                        list
                    ):

                        continue

                    for packet in packets:

                        if not isinstance(
                            packet,
                            dict
                        ):

                            continue

                        command = packet.get(
                            "cmd"
                        )

                        # =================================================
                        # ROOM INFO
                        # =================================================

                        if command == "RoomInfo":

                            print(
                                "Received RoomInfo."
                            )

                            # -----------------------------------------
                            # Get datapackage FIRST.
                            # -----------------------------------------

                            await websocket.send(
                                json.dumps([
                                    {
                                        "cmd":
                                            "GetDataPackage"
                                    }
                                ])
                            )

                            # -----------------------------------------
                            # Now authenticate.
                            #
                            # IMPORTANT:
                            # The name MUST be a real player slot.
                            #
                            # Tracker allows the game field to be empty,
                            # but it still requires a valid slot name.
                            # -----------------------------------------

                            connect_packet = {

                                "cmd":
                                    "Connect",

                                "password":
                                    password
                                    if password
                                    else None,

                                "game":
                                    None,

                                "name":
                                    slot_name,

                                "uuid":
                                    str(
                                        uuid.uuid4()
                                    ),

                                "version":
                                    PROTOCOL_VERSION,

                                "tags":
                                    [
                                        "Tracker"
                                    ],

                                "items_handling":
                                    0,

                                "slot_data":
                                    False,
                            }

                            await websocket.send(
                                json.dumps([
                                    connect_packet
                                ])
                            )

                        # =================================================
                        # DATA PACKAGE
                        # =================================================

                        elif command == "DataPackage":

                            load_datapackage(
                                packet.get(
                                    "data",
                                    {}
                                )
                            )

                        # =================================================
                        # CONNECTED
                        # =================================================

                        elif command == "Connected":

                            print()
                            print(
                                "=========================================="
                            )

                            print(
                                "Tracker connected successfully!"
                            )

                            print(
                                "=========================================="
                            )

                            update_players(
                                packet.get(
                                    "players",
                                    []
                                )
                            )

                            update_slot_games(
                                packet.get(
                                    "slot_info",
                                    {}
                                )
                            )

                            print(
                                f"Players loaded: "
                                f"{len(player_names)}"
                            )

                            print()

                        # =================================================
                        # ROOM UPDATE
                        # =================================================

                        elif command == "RoomUpdate":

                            update_players(
                                packet.get(
                                    "players",
                                    []
                                )
                            )

                        # =================================================
                        # PRINTJSON
                        # =================================================

                        elif command == "PrintJSON":

                            if packet.get(
                                "type"
                            ) == "ItemSend":

                                await process_item_send(
                                    packet
                                )

                        # =================================================
                        # CONNECTION REFUSED
                        # =================================================

                        elif command == "ConnectionRefused":

                            errors = packet.get(
                                "errors",
                                []
                            )

                            print()
                            print(
                                "Archipelago refused "
                                "the tracker connection."
                            )

                            for error in errors:

                                print(
                                    f"  - {error}"
                                )

                            print()

                            # Invalid slot means the name supplied
                            # doesn't exist in this multiworld.
                            if (
                                "InvalidSlot"
                                in errors
                            ):

                                print(
                                    "The slot name is invalid."
                                )

                                print(
                                    "Restart the tracker and "
                                    "enter an EXACT player slot name."
                                )

                                return

                            await websocket.close()

                            break

        except Exception as error:

            print()
            print(
                "Archipelago connection lost."
            )

            print(
                f"Reason: {error}"
            )

            print(
                "Retrying in 5 seconds..."
            )

            await asyncio.sleep(
                5
            )


# ============================================================
# HTTP SERVER
# ============================================================

class TrackerHTTPHandler(
    SimpleHTTPRequestHandler
):

    def __init__(
        self,
        *args,
        **kwargs
    ):

        super().__init__(
            *args,
            directory=str(
                Path(__file__).parent
            ),
            **kwargs
        )

    def log_message(
        self,
        format,
        *args
    ):

        pass


def start_http_server():

    server = ThreadingHTTPServer(
        (
            HTTP_HOST,
            HTTP_PORT
        ),
        TrackerHTTPHandler
    )

    print(
        f"Tracker webpage:"
    )

    print(
        f"http://{HTTP_HOST}:{HTTP_PORT}"
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

async def main():

    print()
    print(
        "=========================================="
    )

    print(
        "     Archipelago Live Item Tracker"
    )

    print(
        "=========================================="
    )

    print()

    # --------------------------------------------------------
    # Ask which Archipelago server to connect to.
    #
    # Works for a local server (127.0.0.1:38281) or a public
    # lobby (e.g. archipelago.gg:38281) — with or without a
    # ws:// / wss:// prefix.
    # --------------------------------------------------------

    server_address = input(
        f"Enter the Archipelago server address "
        f"(blank for {AP_HOST}:{AP_PORT}): "
    ).strip()

    server_uri = parse_server_address(
        server_address
    )

    print()

    # --------------------------------------------------------
    # Ask for a room password, if the lobby has one.
    # --------------------------------------------------------

    room_password = input(
        "Enter the room password "
        "(blank if none): "
    ).strip()

    print()

    # --------------------------------------------------------
    # Ask which AP slot the tracker should use.
    # --------------------------------------------------------

    slot_name = input(
        "Enter an EXACT player slot name "
        "from your Archipelago multiworld: "
    ).strip()

    if not slot_name:

        print(
            "No slot name entered."
        )

        return

    print()

    # --------------------------------------------------------
    # HTTP server
    # --------------------------------------------------------

    http_thread = threading.Thread(
        target=start_http_server,
        daemon=True
    )

    http_thread.start()

    # --------------------------------------------------------
    # Browser WebSocket
    # --------------------------------------------------------

    browser_server = await websockets.serve(
        browser_websocket,
        BROWSER_WS_HOST,
        BROWSER_WS_PORT
    )

    print(
        f"Browser WebSocket:"
        f" ws://{BROWSER_WS_HOST}:{BROWSER_WS_PORT}"
    )

    print()

    # --------------------------------------------------------
    # Open browser
    # --------------------------------------------------------

    threading.Timer(
        1.0,
        lambda:
            webbrowser.open(
                f"http://{HTTP_HOST}:{HTTP_PORT}"
            )
    ).start()

    # --------------------------------------------------------
    # Archipelago
    # --------------------------------------------------------

    try:

        await connect_to_archipelago(
            slot_name,
            server_uri,
            room_password
        )

    finally:

        browser_server.close()

        await browser_server.wait_closed()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print()
        print(
            "Tracker stopped."
        )