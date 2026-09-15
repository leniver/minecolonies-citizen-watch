# Citizen watch

Unofficial live panel for following MineColonies citizens on a local server: meals, hunger, state and colony arrivals, read over RCON.

It was built to investigate how MineColonies records the food history of citizens, and grew into a general tool for watching a few citizens closely while you play. It runs on your machine, talks to your own server, and shows everything in a browser page.

Citizen watch is not made by or affiliated with the MineColonies team.

## What it shows

- **Citizen page:** the last 10 meals with back-to-back repeats highlighted, current AI state and job state, health, position, hunger bar with the point where a citizen goes to eat, and a timeline of every change (state, food history, saturation, joining or leaving the colony).
- **Followed citizens:** a board of cards for every citizen you follow, updated live, with state, hunger, meals and the latest change. Followed citizens keep being read even when the page is closed.
- **Colony:** the number of citizens and a collapsible list of recent arrivals and departures.
- **Journal:** everything the panel learns is appended to `data/journal.jsonl` and loaded again on start, so a restart or a closed tab loses nothing.

## What it changes in game

Reading is passive. Only these buttons change anything:

- **Make hungry** and **Make everyone hungry** set saturation to 2, just under the point where a citizen goes to eat, so you can trigger a meal on demand.
- **Clear food history** and **Clear everyone's food history** empty the in-game food history, as if the citizens had never eaten. They need a MineColonies build with the `/mc citizens modify <colony> <citizen> foodHistory clear` subcommand, which no official release has yet; the panel says so when the server lacks it.
- **Locate** outlines a citizen with the glowing effect for a chosen number of real seconds (30 by default), like the tracking in the MineColonies colony map. Every player on the server sees the outline.

## Requirements

- Python 3.9 or newer. Only the standard library is used, there is nothing to install.
- A MineColonies dedicated server on the same machine, with RCON enabled. Single player has no RCON. Tested with MineColonies 1.1.1387 for Minecraft 1.21.1 on NeoForge.
- For saturation readings and **Make hungry**: a player from the server's `ops.json` connected in Creative. MineColonies only accepts `/mc citizens modify` from the server console or a player in Creative, so the panel runs it as the first connected operator.
- A browser. The page loads the Barlow Semi Condensed font from Google Fonts and falls back to a system font without internet access.

## Setting up the server

In `server.properties`:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=choose-a-long-random-password
broadcast-rcon-to-ops=false
```

`broadcast-rcon-to-ops=false` keeps the panel's commands out of the operators' chat. The panel reads the port and password from `server.properties` and only connects to `127.0.0.1`.

To use a world you normally play in single player, a NeoForge server can open it in place with `--universe <saves folder> --world <world name>`. Only one process can open a world at a time, so leave it in single player before starting the server.

If you speed the game up with `/tick rate`, the server watchdog may stop a dedicated server that cannot keep up. `max-tick-time=-1` in `server.properties` turns the watchdog off.

## Running

```sh
python3 panel.py --server-dir /path/to/server --world-dir /path/to/world
```

Then open http://127.0.0.1:8765.

| Option | What it is for |
| --- | --- |
| `--server-dir` | Dedicated server folder with `server.properties` and `ops.json`. Enables live readings, the citizen list and the buttons. |
| `--world-dir` | World folder holding the colony save, read after every autosave. Defaults to the server's `level-name` folder when it exists. |
| `--game-dir` | Game client folder. Its `logs/latest.log` is followed for `/mc citizens info` output you type in chat, and its older logs are imported once when the journal is created. |
| `--port` | Port for the page, 8765 by default. |
| `--follow` | Citizen IDs to read for this run, for example `15,43`. The **Keep following** button on a citizen page is remembered instead. |
| `--journal` | Where the journal is kept, `data/journal.jsonl` by default. Delete it to start from scratch. |

At least one of `--server-dir`, `--world-dir` or `--game-dir` is required.

## Settings

The **Settings** button next to the title sets:

- **Read from the server every:** from 1 to 600 game ticks. The panel reads the server's tick rate, so the interval stays in step with the game when you change `/tick rate`.
- **Locate makes a citizen glow for:** 10 seconds to 5 minutes of real time, whatever the tick rate.
- **Clear history:** starts fresh by forgetting every timeline, meal history and former citizen. Settings and followed citizens are kept, and the old journal is renamed to `data/journal-archive-<date>-<time>.jsonl` rather than deleted.

## Load on the server

Each followed citizen costs two RCON commands per read, and the full citizen list is read every 10 seconds. On a server running at 20 ticks per second, a command took about 0.6 ms, and following 5 citizens every 2 ticks made no measurable difference to tick times. At high tick rates the server has no spare time between ticks, so commands take time from the game: follow fewer citizens or read less often.

## Known limits

- `/mc citizens info` fails on a citizen with an empty food history (MineColonies throws before printing it). The panel shows that as an empty history.
- The server collects the output of all RCON connections in one buffer, so commands sent at the same time get each other's replies. The panel sends its own commands one at a time, but another RCON tool used while the panel runs can still garble replies on both sides.
- The citizen list is read page by page, so a list read while citizens join or leave, or garbled as above, can miss people. The panel only uses a list that matches the colony's own count, and only counts a citizen as gone after two readings without them. It cannot tell a death from a dismissal.
- MineColonies gives a new citizen the lowest free ID, so an ID can belong to several citizens over time. The panel archives a citizen when they leave and starts a new record for whoever takes their ID; former citizens stay listed with their history. A name change seen within a minute of the previous citizen list counts as a rename, so a citizen who leaves and is replaced faster than that would be taken for a rename.
- Locate needs the citizen to be loaded, which means a player nearby.
- The colony save is only as fresh as the last world autosave.

## License

MIT, see [LICENSE](LICENSE).
