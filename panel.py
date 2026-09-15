#!/usr/bin/env python3
"""Citizen watch: a local live panel for following MineColonies citizens.

Sources, each optional:
  --server-dir     a dedicated server with RCON: the panel reads /mc citizens info, the
                   saturation and the citizen list at the interval set on the page, for the
                   citizen you are viewing and every citizen you follow
  --world-dir      the colony save, data/minecolonies_colony_manager.dat, rewritten on every
                   world autosave (the minecolonies/<dim>/colony<N>.dat copies are a fallback)
  --game-dir       a game client's logs/latest.log, for /mc citizens info typed in chat, plus
                   its older logs, imported once when the journal is created

Only the buttons change anything in game: Make hungry sets a citizen's saturation, Locate
gives them a short glowing effect.

Everything the panel learns is appended to data/journal.jsonl and replayed on start, so a
restart or a closed tab loses nothing.
"""
import argparse
import glob
import gzip
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import nbt
from rcon import Rcon, RconError

HERE = Path(__file__).resolve().parent
MAX_EVENTS = 5000          # per citizen, in memory; the journal keeps everything
WATCH_SECONDS = 10          # keep polling a citizen this long after the page last asked for it
MEAL_JUMP = 5.0             # saturation rise that counts as a meal in the timeline
EAT_THRESHOLD = 2.5
SATURATION_STEP = 0.5       # smallest saturation change worth a journal line
POLL_TICKS = (1, 2, 5, 10, 20, 40, 100, 200, 600)  # choices offered on the page, in game ticks
DEFAULT_POLL_TICKS = 20
VANILLA_TICK_RATE = 20.0    # used until the server's own rate has been read
TICK_RATE_SECONDS = 5       # how often /tick query is asked for the current rate
GLOW_CHOICES = (10, 30, 60, 120, 300)  # Locate outline lengths offered on the page, in real seconds
DEFAULT_GLOW_SECONDS = 30   # as long as MineColonies' own colony map tracking highlight
GLOW_SLACK = 1.5            # the effect gets this much extra game time; the panel clears it at the exact real time
ROSTER_SECONDS = 10         # how often the full citizen list is read over RCON
NEW_CITIZEN_MINUTES = 30    # a citizen who joined within this long is shown as new
INFO_KEYS = ('loaded', 'health', 'job', 'state', 'job_ai', 'job_state', 'stuck', 'food')
MONTHS = {m: i for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}

lock = threading.Lock()
citizens = {}
names = {}
watched = {}
following = set()
roster = {}                 # citizen id -> name, everyone in the colony at the last roster reading
missing_since = {}          # citizen id -> first reading they were missing from; they only count as gone after a second one
wake_poller = threading.Event()  # set to cut the RCON poller's wait short
glows = {}                  # citizen id -> {'until': real end time, 'rate': tick rate the effect was sized for, 'colony': id}
glow_lock = threading.Lock()
meta = {'log_path': None, 'save_path': None, 'log_updated': None, 'save_updated': None, 'rcon_updated': None,
        'log_error': None, 'save_error': None, 'rcon_status': 'disabled', 'colony': None, 'colony_id': None,
        'journal_path': None, 'journal_error': None, 'operators_online': [],
        'citizen_count': None, 'roster_t': None, 'roster_source': None,
        'poll_ticks': DEFAULT_POLL_TICKS, 'poll_ticks_choices': list(POLL_TICKS), 'tick_rate': None,
        'poll_interval': DEFAULT_POLL_TICKS / VANILLA_TICK_RATE, 'poll_loop_seconds': None,
        'glow_seconds': DEFAULT_GLOW_SECONDS, 'glow_choices': list(GLOW_CHOICES)}
server_dir = None           # dedicated server folder, for one-off RCON commands from the page
HUNGRY_SATURATION = 2.0     # just under the 2.5 at which a citizen goes to eat

journal = None              # open append handle to data/journal.jsonl
replaying = False           # True while rebuilding state from the journal: nothing is written back
journaled_info = {}         # citizen id -> last journaled INFO_KEYS values
journaled_saturation = {}   # citizen id -> last journaled saturation

TS_RE = re.compile(r'^\[(\d{2})([A-Za-z]{3})(\d{4}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})\]')
CHAT_RE = re.compile(r'\[CHAT\] (.*)$')
CODES_RE = re.compile('§.')
EMPTY_HISTORY_ERROR = 'Range [0, -2) out of bounds for length 0'


def parse_ts(match):
    day, mon, year, hh, mm, ss, ms = match.groups()
    return datetime(int(year), MONTHS.get(mon, 1), int(day), int(hh), int(mm), int(ss), int(ms) * 1000)


def iso(dt):
    return dt.isoformat(timespec='milliseconds') if dt else None


def food_name(item_id):
    return item_id.split(':', 1)[-1].replace('_', ' ').title()


def citizen(cid, name=None):
    record = citizens.setdefault(cid, {'id': cid, 'name': f'#{cid}', 'log': {}, 'save': {}, 'events': []})
    if name:
        record['name'] = name
        names[name] = cid
    return record


def write_journal(entry):
    """Append one line to the journal. Callers hold the lock."""
    if journal is None or replaying:
        return
    try:
        journal.write(json.dumps(entry, ensure_ascii=False) + '\n')
        journal.flush()
        meta['journal_error'] = None
    except OSError as error:
        meta['journal_error'] = f'Could not write the journal: {error}'


def add_event(record, when, kind, text, before=None, after=None):
    if replaying:
        return  # replay restores events from their own journal lines
    event = {'t': iso(when), 'kind': kind, 'text': text, 'before': before, 'after': after}
    record['events'].append(event)
    del record['events'][:-MAX_EVENTS]
    write_journal({'type': 'event', 'id': record['id'], **event})


def is_older(ts, last_iso):
    """Readings older than what is already known come from re-reading a log after a restart."""
    return last_iso is not None and iso(ts) < last_iso


# ------------------------------------------------------------------------------------- shared parsing


def parse_field(fields, msg):
    """Parse one message of /mc citizens info into fields. Returns True for the food line, the last one."""
    if m := re.match(r'Citizen position: x=(-?\d+) y=(-?\d+) z=(-?\d+)', msg):
        fields['position'] = [int(v) for v in m.groups()]
    elif m := re.match(r'Health: (-?[\d.]+) Max Health: (-?[\d.]+)', msg):
        fields['health'] = [float(m.group(1)), float(m.group(2))]
    elif m := re.match(r'Job: (\S+)$', msg):
        fields['job'] = m.group(1).rsplit('.', 1)[-1]
    elif msg.startswith('Citizen state: Current state:'):
        fields['loaded'] = True
        # In the log the rest of this message is on the same line with escaped newlines; over RCON it is not.
        parts = msg.split('\\n')
        fields['state'] = parts[0].split('Current state:', 1)[1].strip()
        fields['ai_history'] = []
        for part in parts[1:]:
            parse_state_continuation(fields, part.strip())
    elif 'Jobstate: Current state:' in msg or re.match(r'\d{2}:\d{2}:\d{2} ', msg):
        parse_state_continuation(fields, msg)
    elif msg.startswith('Citizen entity not loaded'):
        fields['loaded'] = False
    elif m := re.match(r'Stuck level: (-?\d+)', msg):
        fields['stuck'] = int(m.group(1))
    elif m := re.match(r'Full food history: (\w+), Quality: (\d+), Diversity: (\d+), Last Eaten: (.*)$', msg):
        fields['food'] = {'full': m.group(1) in ('1', 'true'), 'quality': int(m.group(2)),
                          'diversity': int(m.group(3)),
                          'items': [s.strip() for s in m.group(4).split(',') if s.strip()]}
        return True
    return False


def parse_state_continuation(fields, text):
    if not text:
        return
    if jm := re.search(r'Job: (\S+) Jobstate: Current state:(\S+)', text):
        fields['job_ai'] = jm.group(1)
        fields['job_state'] = jm.group(2)
    elif re.match(r'\d{2}:\d{2}:\d{2} ', text):
        fields.setdefault('ai_history', []).append(text)


def apply_saturation(record, ts, value, source):
    log = record['log']
    if is_older(ts, log.get('saturation_t')):
        return
    previous = log.get('saturation')
    last_written = journaled_saturation.get(record['id'])
    if last_written is None or abs(value - last_written) >= SATURATION_STEP:
        write_journal({'type': 'saturation', 'id': record['id'], 't': iso(ts), 'value': value, 'source': source})
        journaled_saturation[record['id']] = value
    log['saturation'] = value
    log['saturation_t'] = iso(ts)
    log['saturation_source'] = source
    if previous is None:
        return
    if value - previous >= MEAL_JUMP:
        add_event(record, ts, 'hunger', f'Saturation {previous:.1f} to {value:.1f}, a meal')
    elif previous > EAT_THRESHOLD >= value:
        add_event(record, ts, 'hunger', f'Saturation dropped to {value:.1f}, hungry enough to eat')


def apply_info(cid, name, ts, fields, source):
    record = citizen(cid, name)
    log = record['log']
    if 'position' in fields:
        log['position'] = fields['position']
    if 'state' not in fields and 'food' not in fields:
        return  # a /mc citizens list line, not an info reading
    if is_older(ts, log.get('t')):
        return

    if 'state' in fields and log.get('state') and log['state'] != fields['state']:
        add_event(record, ts, 'state', f"{log['state']} to {fields['state']}")
    if 'job_state' in fields and log.get('job_state') and log['job_state'] != fields['job_state']:
        add_event(record, ts, 'job', f"{log['job_state']} to {fields['job_state']}")
    if 'food' in fields:
        previous = log.get('food')
        if previous is not None and previous['items'] != fields['food']['items']:
            add_event(record, ts, 'food', 'Food history changed', previous['items'], fields['food']['items'])
        elif previous is None:
            add_event(record, ts, 'food-first', 'First food history reading', None, fields['food']['items'])
    log['loaded'] = fields.get('loaded', log.get('loaded'))
    for key in ('health', 'job', 'state', 'job_ai', 'job_state', 'ai_history', 'stuck', 'food'):
        if key in fields:
            log[key] = fields[key]
    log['t'] = iso(ts)
    log['source'] = source
    if not replaying:
        meta['rcon_updated' if source == 'rcon' else 'log_updated'] = iso(ts)
    # Journal a reading only when something that matters changed; position and AI history churn every second.
    signature = {key: log.get(key) for key in INFO_KEYS}
    if journaled_info.get(cid) != signature:
        write_journal({'type': 'info', 'id': cid, 'name': record['name'], 't': iso(ts), 'source': source, 'fields': fields})
        journaled_info[cid] = signature


# ------------------------------------------------------------------------------------------------ log


class LogParser:
    def __init__(self, source='log'):
        self.source = source
        self.block = None
        self.last_ts = None
        self.last_empty_error = None

    def feed(self, line):
        ts_match = TS_RE.match(line)
        if ts_match:
            self.last_ts = parse_ts(ts_match)
        ts = self.last_ts

        if EMPTY_HISTORY_ERROR in line:
            # The server logs this a few milliseconds before the client receives the command's chat lines.
            self.last_empty_error = ts
            return

        chat = CHAT_RE.search(line)
        if not chat or ts is None:
            return
        msg = CODES_RE.sub('', chat.group(1)).strip()

        head = re.match(r'ID:\s+(\d+)\s+Name:\s+(.*)$', msg)
        if head:
            self.close()
            recent_error = self.last_empty_error is not None and 0 <= (ts - self.last_empty_error).total_seconds() < 0.5
            self.block = {'id': int(head.group(1)), 'name': head.group(2).strip(), 't': ts, 'opened': time.time(),
                          'fields': {}, 'empty_error': recent_error}
            return

        if sat := re.match(r'The saturation of (.+) is now (-?[\d.]+)\.$', msg):
            cid = names.get(sat.group(1).strip())
            if cid is not None:
                apply_saturation(citizen(cid), ts, float(sat.group(2)), self.source)
            return

        if self.block is not None and parse_field(self.block['fields'], msg):
            self.close()

    def close(self):
        block, self.block = self.block, None
        if not block:
            return
        fields = block['fields']
        if 'food' not in fields and 'state' in fields and block['empty_error']:
            fields['food'] = {'full': False, 'quality': 0, 'diversity': 0, 'items': []}
        apply_info(block['id'], block['name'], block['t'], fields, self.source)

    def flush_stale(self):
        if self.block and time.time() - self.block['opened'] > 1.5:
            self.close()


def follow_log(path):
    parser = LogParser()
    handle, inode, position = None, None, 0
    while True:
        try:
            stat = os.stat(path)
            if handle is None or stat.st_ino != inode or stat.st_size < position:
                if handle:
                    handle.close()
                handle = open(path, 'r', encoding='utf-8', errors='replace')
                inode, position = stat.st_ino, 0
            handle.seek(position)
            lines = handle.readlines()
            position = handle.tell()
            with lock:
                for line in lines:
                    parser.feed(line)
                parser.flush_stale()
                meta['log_error'] = None
        except FileNotFoundError:
            meta['log_error'] = 'Log file not found. Start the game once to create it.'
            handle = None
        except Exception as error:  # keep following even if one read fails
            meta['log_error'] = f'Could not read the log: {error}'
        time.sleep(0.5)


def import_old_logs(logs_dir):
    """Read the rotated client logs once, oldest first. They only hold readings someone typed in chat."""
    files = sorted(Path(logs_dir).glob('[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*.log.gz'), key=lambda f: f.stat().st_mtime)
    for path in files:
        parser = LogParser(source='old-log')
        try:
            with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    parser.feed(line)
            parser.close()
        except (OSError, EOFError) as error:
            meta['log_error'] = f'Could not import {path.name}: {error}'
    write_journal({'type': 'import', 't': iso(datetime.now()), 'files': [f.name for f in files]})
    return len(files)


# ----------------------------------------------------------------------------------------------- rcon


def read_server_properties(server_dir):
    props = {}
    with open(Path(server_dir) / 'server.properties', encoding='utf-8') as handle:
        for line in handle:
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.rstrip('\n').split('=', 1)
                props[key.strip()] = value.strip()
    return props


NOT_CREATIVE = 'Must be in Creative Mode'


def operator_names(folder):
    try:
        with open(Path(folder) / 'ops.json', encoding='utf-8') as handle:
            return [op['name'] for op in json.load(handle)]
    except Exception:
        return []


def online_operators(client, folder):
    """Operators from ops.json who are connected right now, in ops.json order."""
    reply = CODES_RE.sub('', client.command('list'))
    online = {name.strip().lower() for name in reply.split(':', 1)[-1].split(',') if name.strip()}
    return [name for name in operator_names(folder) if name.lower() in online]


def rcon_client(folder):
    props = read_server_properties(folder)
    if props.get('enable-rcon') != 'true':
        return None
    return Rcon('127.0.0.1', int(props.get('rcon.port', 25575)), props.get('rcon.password', ''))


def server_tick_rate(client):
    """Target ticks per second from /tick query ("Target tick rate: 20,0 per second"; the decimal mark follows the server locale)."""
    m = re.search(r'Target tick rate:\s*([\d.,]+)', CODES_RE.sub('', client.command('tick query')))
    try:
        return float(m.group(1).replace(',', '.')) if m else None
    except ValueError:
        return None


def colony_citizen_count(client, colony):
    """The colony's own citizen count, from /mc colony info ("Citizens: 113/150")."""
    m = re.search(r'Citizens:\s*(\d+)\s*/', CODES_RE.sub('', client.command(f'mc colony info {colony}')))
    return int(m.group(1)) if m else None


def colony_citizens(client, colony):
    """Every living citizen as {id: name}, read from the server's paged citizen list."""
    members, page, pages = {}, 1, 1
    while page <= pages:
        text = CODES_RE.sub('', client.command(f'mc citizens list {colony} {page}'))
        if m := re.search(r'page \d+ of (\d+)', text):
            pages = int(m.group(1))
        for cid, name in re.findall(r'ID:\s+(\d+)\s+Name:\s+(.+)', text):
            members[int(cid)] = name.strip()
        page += 1
    return members


def make_hungry(cids=None):
    """Set saturation just below the eating threshold for these citizens, or for all of them when cids is None.
    Returns (ok, message)."""
    if not server_dir:
        return False, 'The panel is not connected to a local server.'
    client = rcon_client(server_dir)
    if client is None:
        return False, 'RCON is not enabled on the local server.'
    colony = meta.get('colony_id') or 1
    done, failed, not_creative, operators, player = [], [], [], [], None
    try:
        client.connect()
        operators = online_operators(client, server_dir)
        if cids is None:
            cids = sorted(colony_citizens(client, colony))
        # The modify command only runs for the server console or a player in Creative, so run it as a connected operator.
        candidates = list(operators)
        for cid in cids:
            while candidates:
                reply = CODES_RE.sub('', client.command(
                    f'execute as {candidates[0]} run mc citizens modify {colony} {cid} saturation = {HUNGRY_SATURATION}')).strip()
                if NOT_CREATIVE not in reply:
                    break
                not_creative.append(candidates.pop(0))
            if not candidates:
                break
            player = candidates[0]
            if sat := re.search(r'is now (-?[\d.]+)\.', reply):
                done.append((cid, float(sat.group(1))))
            else:
                failed.append((cid, reply or 'no answer'))
    except ConnectionRefusedError:
        return False, 'The local server is offline.'
    except (OSError, RconError) as error:
        return False, f'Could not reach the server: {error}'
    finally:
        client.close()

    if done:
        ts = datetime.now()
        with lock:
            for cid, value in done:
                record = citizen(cid)
                add_event(record, ts, 'action', f'Made hungry from the panel as {player}, saturation set to {value:.1f}')
                apply_saturation(record, ts, value, 'rcon')
    if not operators:
        known = ', '.join(operator_names(server_dir)) or 'nobody, ops.json is empty'
        return False, f'No operator is connected. Join the server as an operator ({known}) to use this.'
    if not done and not_creative and len(not_creative) == len(operators):
        return False, f'{", ".join(not_creative)} {"is" if len(not_creative) == 1 else "are"} not in Creative. Switch to Creative and try again.'
    if len(cids) == 1 and not failed and done:
        return True, f'Saturation set to {done[0][1]:.1f} as {player}. They go to eat on their next check.'
    if len(cids) == 1:
        return False, failed[0][1] if failed else 'The server gave no answer.'
    message = f'{len(done)} of {len(cids)} citizens set to saturation {HUNGRY_SATURATION:.0f} as {player}.'
    if failed:
        message += f' Failed for {", ".join(f"#{cid}" for cid, _ in failed)}: {failed[0][1]}'
    return bool(done), message


def glow_selector(cid, colony):
    return f'@e[type=minecolonies:citizen,nbt={{citizen:{cid},colony:{colony}}},limit=1]'


def glow_effect_seconds(real_seconds, rate):
    """/effect counts game seconds (20 ticks each): size the effect for the real time at this tick rate, plus slack."""
    return max(1, math.ceil(real_seconds * rate / VANILLA_TICK_RATE * GLOW_SLACK))


def locate_citizen(cid):
    """Outline a citizen in game for the chosen real time, like the colony map's tracking. Returns (ok, message)."""
    if not server_dir:
        return False, 'The panel is not connected to a local server.'
    client = rcon_client(server_dir)
    if client is None:
        return False, 'RCON is not enabled on the local server.'
    colony = meta.get('colony_id') or 1
    selector = glow_selector(cid, colony)
    real_seconds = meta['glow_seconds']
    try:
        client.connect()
        rate = server_tick_rate(client) or meta.get('tick_rate') or VANILLA_TICK_RATE  # fresh, not up to 5 s old
        # Clear first: /effect give keeps an existing longer glow instead of shortening it.
        client.command(f'effect clear {selector} minecraft:glowing')
        reply = CODES_RE.sub('', client.command(
            f'effect give {selector} minecraft:glowing {glow_effect_seconds(real_seconds, rate)} 0 true')).strip()
    except ConnectionRefusedError:
        return False, 'The local server is offline.'
    except (OSError, RconError) as error:
        return False, f'Could not reach the server: {error}'
    finally:
        client.close()
    if reply.startswith('Applied effect'):
        with glow_lock:
            glows[cid] = {'until': time.time() + real_seconds, 'rate': rate, 'colony': colony}
        return True, f'Glowing for {real_seconds} s.'
    if 'No entity was found' in reply:
        return False, 'Not loaded: nobody is near this citizen, so there is nothing to outline.'
    return False, reply or 'The server gave no answer.'


def keep_glows(folder):
    """End each Locate glow at its exact real time, and resize it when the tick rate changes while it runs."""
    client = rcon_client(folder)
    if client is None:
        return
    last_rate_check, rate = 0.0, None
    while True:
        time.sleep(0.2)
        with glow_lock:
            pending = dict(glows)
        if not pending:
            continue
        now = time.time()
        try:
            if now - last_rate_check >= 1.0:
                rate = server_tick_rate(client) or rate
                last_rate_check = now
            for cid, glow in pending.items():
                selector = glow_selector(cid, glow['colony'])
                if now >= glow['until']:
                    client.command(f'effect clear {selector} minecraft:glowing')
                    with glow_lock:
                        if glows.get(cid) is glow:
                            del glows[cid]
                elif rate and rate != glow['rate']:
                    client.command(f'effect clear {selector} minecraft:glowing')
                    client.command(f'effect give {selector} minecraft:glowing {glow_effect_seconds(glow["until"] - now, rate)} 0 true')
                    glow['rate'] = rate
        except (OSError, RconError):
            time.sleep(2)  # server gone: the slack lets the effect run out by itself


def poll_rcon(server_dir):
    client = rcon_client(server_dir)
    if client is None:
        meta['rcon_status'] = 'disabled'
        return
    last_roster = 0.0
    last_operators, operators = 0.0, []
    last_tick_rate = 0.0
    while True:
        now = time.time()
        with lock:
            ids = sorted(following | {cid for cid, seen in watched.items() if now - seen < WATCH_SECONDS})
            colony = meta.get('colony_id') or 1
        try:
            if now - last_operators >= 1.0 or not ids:  # once a second is plenty, even at a 100 ms interval
                operators = online_operators(client, server_dir)  # also checks the server is reachable
                meta['operators_online'] = operators
                last_operators = now
            player = operators[0] if operators else None
            if now - last_tick_rate >= TICK_RATE_SECONDS:
                rate = server_tick_rate(client)
                with lock:
                    meta['tick_rate'] = rate
                    update_poll_interval()
                last_tick_rate = now
            if now - last_roster >= ROSTER_SECONDS:
                # The list is paged over a HashMap, so citizens added between page requests can shift the order and
                # make a reading skip or repeat people. Only trust a reading that matches the colony's own count.
                count_before = colony_citizen_count(client, colony)
                members = colony_citizens(client, colony)
                count_after = colony_citizen_count(client, colony)
                with lock:
                    if count_after is not None:
                        meta['citizen_count'] = count_after
                    if members and count_before == count_after == len(members):
                        apply_roster(members, datetime.now(), 'rcon')
                    else:
                        meta['roster_skipped'] = iso(datetime.now())
                last_roster = now
            for cid in ids:
                text = CODES_RE.sub('', client.command(f'mc citizens info {colony} {cid}'))
                ts = datetime.now()
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                head = re.match(r'ID:\s+(\d+)\s+Name:\s+(.*)$', lines[0]) if lines else None
                if head:
                    fields = {}
                    for line in lines[1:]:
                        parse_field(fields, line)
                    if 'food' not in fields:
                        # The command throws on an empty history before printing the food line.
                        fields['food'] = {'full': False, 'quality': 0, 'diversity': 0, 'items': []}
                    # The modify command only runs for the server console or a player in Creative, so run it as the operator.
                    reply = CODES_RE.sub('', client.command(f'execute as {player} run mc citizens modify {colony} {cid} saturation + 0')) if player else ''
                    with lock:
                        apply_info(int(head.group(1)), head.group(2).strip(), ts, fields, 'rcon')
                        if sat := re.search(r'is now (-?[\d.]+)\.', reply):
                            apply_saturation(citizen(int(head.group(1))), ts, float(sat.group(1)), 'rcon')
            meta['rcon_status'] = 'connected'
            meta['poll_loop_seconds'] = round(time.time() - now, 3)
            # The interval counts from the start of this round, so reading many citizens does not add to it.
            wake_poller.wait(max(0.005, meta['poll_interval'] - (time.time() - now)))
            wake_poller.clear()
        except ConnectionRefusedError:
            meta['rcon_status'] = 'offline'
            meta['operators_online'] = []
            time.sleep(3)
        except (OSError, RconError) as error:
            meta['rcon_status'] = f'error: {error}'
            meta['operators_online'] = []
            time.sleep(3)


# ----------------------------------------------------------------------------------------------- save


def apply_colony(name, colony_id):
    if (meta.get('colony'), meta.get('colony_id')) != (name, colony_id):
        write_journal({'type': 'colony', 'name': name, 'colony_id': colony_id})
    meta['colony'] = name
    meta['colony_id'] = colony_id


def apply_save_entry(cid, name, entry):
    record = citizen(cid, name)
    save = record['save']
    if is_older(datetime.fromisoformat(entry['t']), save.get('t')):
        return
    if save and save.get('foods') != entry['foods']:
        add_event(record, datetime.fromisoformat(entry['t']), 'save-food', 'Food history changed in the colony save',
                  save.get('foods'), entry['foods'])
    if {k: v for k, v in save.items() if k != 't'} != {k: v for k, v in entry.items() if k != 't'}:
        write_journal({'type': 'save', 'id': cid, 'name': record['name'], 'entry': entry})
    save.clear()
    save.update(entry)


def apply_roster(members, ts, source):
    """Compare everyone in the colony now with the last reading and record who joined or left."""
    if is_older(ts, meta.get('roster_t')):
        return
    known = meta.get('roster_t') is not None
    missing = set(roster) - set(members) if known else set()
    for cid in list(missing_since):
        if cid not in missing:
            del missing_since[cid]  # back in the list, so the earlier reading was wrong
    # The journal only holds confirmed rosters, so a replayed departure needs no second reading.
    gone = missing if replaying else {cid for cid in missing if cid in missing_since}
    for cid in missing - gone:
        missing_since.setdefault(cid, ts)
    current = dict(members)
    current.update({cid: roster[cid] for cid in missing - gone})  # still counted until confirmed
    if known:
        for cid in sorted(set(members) - set(roster)):
            add_event(citizen(cid, members[cid]), ts, 'joined', 'Joined the colony')
        for cid in sorted(gone):
            add_event(citizen(cid, roster[cid]), missing_since.pop(cid, ts), 'left', 'Left the colony')
    if current != roster:
        write_journal({'type': 'roster', 't': iso(ts), 'source': source, 'members': {str(k): v for k, v in current.items()}})
        for cid, name in current.items():
            citizen(cid, name)
    roster.clear()
    roster.update(current)
    if source != 'rcon' or meta.get('citizen_count') is None:
        meta['citizen_count'] = len(members)
    meta['roster_t'] = iso(ts)
    meta['roster_source'] = source


def apply_save(root, mtime):
    when = datetime.fromtimestamp(mtime)
    apply_colony(root.get('name'), root.get('id'))
    saved = root.get('citizenManager', {}).get('citizens', [])
    if saved:
        apply_roster({rec['id']: rec.get('name', f"#{rec['id']}") for rec in saved}, when, 'save')
    for rec in saved:
        foods = [food_name(x) for x in rec.get('lastfoods', [])]
        job = rec.get('job') or {}
        entry = {
            't': iso(when),
            'saturation': round(float(rec.get('saturation', 0.0)), 2),
            'just_ate': bool(rec.get('justAte', 0)),
            'foods': foods,
            'job': job.get('type', '').split(':')[-1] or None,
            'inventory_stacks': len(rec.get('inventory', [])),
            'inventory_size': rec.get('invsize'),
        }
        apply_save_entry(rec['id'], rec.get('name'), entry)
    meta['save_updated'] = iso(when)


def colonies_in(node):
    """Every colony compound in a save tree: the manager file holds all colonies, a colony<N>.dat just one."""
    if isinstance(node, dict):
        if 'citizenManager' in node:
            yield node
            return
        for value in node.values():
            yield from colonies_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from colonies_in(value)


def follow_save(path_patterns):
    last_seen = None
    pending = None
    while True:
        try:
            files = []
            for pattern in path_patterns:  # first pattern with a match wins
                files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
                if files:
                    break
            if not files:
                meta['save_error'] = 'No colony save found yet. Save the world once (for example with /save-all).'
            else:
                path = files[0]
                stat = os.stat(path)
                signature = (path, stat.st_mtime, stat.st_size)
                if signature != last_seen:
                    if pending != signature:
                        pending = signature  # wait one more poll so a file being written settles
                    else:
                        with tempfile.NamedTemporaryFile(suffix='.dat', delete=False) as tmp:
                            tmp_path = tmp.name
                        shutil.copyfile(path, tmp_path)
                        try:
                            root = nbt.load(tmp_path)
                        finally:
                            os.unlink(tmp_path)
                        colonies = list(colonies_in(root))
                        wanted = meta.get('colony_id')
                        colony = next((c for c in colonies if c.get('id') == wanted), colonies[0] if colonies else None)
                        with lock:
                            if colony is not None:
                                apply_save(colony, stat.st_mtime)
                            meta['save_path'] = path
                            meta['save_error'] = None if colony is not None else 'The save holds no colony yet.'
                        last_seen = signature
        except Exception as error:
            meta['save_error'] = f'Could not read the colony save: {error}'
            pending = None
        time.sleep(2)


# -------------------------------------------------------------------------------------------- journal


def update_poll_interval():
    """Turn the tick setting into a wait in seconds at the server's current tick rate."""
    meta['poll_interval'] = meta['poll_ticks'] / (meta.get('tick_rate') or VANILLA_TICK_RATE)


def set_glow_seconds(seconds):
    if seconds not in GLOW_CHOICES:
        raise ValueError(f'glow must be one of {GLOW_CHOICES}')
    if meta['glow_seconds'] != seconds:
        write_journal({'type': 'setting', 'glow_seconds': seconds, 't': iso(datetime.now())})
    meta['glow_seconds'] = seconds


def set_poll_ticks(ticks):
    if ticks not in POLL_TICKS:
        raise ValueError(f'ticks must be one of {POLL_TICKS}')
    if meta['poll_ticks'] != ticks:
        write_journal({'type': 'setting', 'poll_ticks': ticks, 't': iso(datetime.now())})
    meta['poll_ticks'] = ticks
    update_poll_interval()
    wake_poller.set()


def set_following(cid, on):
    if (cid in following) != on:
        write_journal({'type': 'follow', 'id': cid, 'on': on, 't': iso(datetime.now())})
    (following.add if on else following.discard)(cid)
    wake_poller.set()


def replay_journal(path):
    """Rebuild state from the journal. Returns False when there was no journal yet."""
    global replaying
    if not path.exists():
        return False
    replaying = True
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                    kind = entry['type']
                    ts = datetime.fromisoformat(entry['t']) if entry.get('t') else None
                    if kind == 'event':
                        citizen(entry['id'])['events'].append({key: entry.get(key) for key in ('t', 'kind', 'text', 'before', 'after')})
                    elif kind == 'info':
                        apply_info(entry['id'], entry.get('name'), ts, entry['fields'], entry['source'])
                        journaled_info[entry['id']] = {key: citizens[entry['id']]['log'].get(key) for key in INFO_KEYS}
                    elif kind == 'saturation':
                        apply_saturation(citizen(entry['id']), ts, entry['value'], entry['source'])
                        journaled_saturation[entry['id']] = entry['value']
                    elif kind == 'save':
                        apply_save_entry(entry['id'], entry.get('name'), entry['entry'])
                    elif kind == 'colony':
                        apply_colony(entry.get('name'), entry.get('colony_id'))
                    elif kind == 'follow':
                        set_following(entry['id'], entry['on'])
                    elif kind == 'setting' and 'glow_seconds' in entry:
                        set_glow_seconds(int(entry['glow_seconds']))
                    elif kind == 'setting' and 'poll_ticks' in entry:
                        set_poll_ticks(int(entry['poll_ticks']))
                    elif kind == 'setting' and 'poll_interval' in entry:
                        # Older journals stored seconds; convert at the vanilla rate to the nearest offered choice.
                        wanted = float(entry['poll_interval']) * VANILLA_TICK_RATE
                        set_poll_ticks(min(POLL_TICKS, key=lambda ticks: abs(ticks - wanted)))
                    elif kind == 'roster':
                        apply_roster({int(k): v for k, v in entry['members'].items()}, ts, entry['source'])
                except (ValueError, KeyError, TypeError):
                    continue  # a torn last line after a crash, or a line from a newer panel
    finally:
        replaying = False
    for record in citizens.values():
        record['events'].sort(key=lambda event: event['t'] or '')
        del record['events'][:-MAX_EVENTS]
    return True


def open_journal(path):
    global journal
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with open(path, 'rb') as handle:
            handle.seek(-1, os.SEEK_END)
            torn = handle.read(1) != b'\n'
    else:
        torn = False
    journal = open(path, 'a', encoding='utf-8')
    if torn:
        journal.write('\n')  # keep the next line from joining a half-written one
    meta['journal_path'] = str(path)


# ----------------------------------------------------------------------------------------------- http


def summary(record):
    log_food = (record['log'].get('food') or {}).get('items')
    save_t = record['save'].get('t')
    newer_log = log_food is not None and (not save_t or (record['log'].get('t') or '') >= save_t)
    items = log_food if newer_log else record['save'].get('foods', [])
    dupes = any(a == b for a, b in zip(items, items[1:]))
    times = [t for t in (record['log'].get('t'), record['save'].get('t')) if t]
    joined = [e['t'] for e in record['events'] if e['kind'] == 'joined']
    return {'id': record['id'], 'name': record['name'],
            'job': record['log'].get('job') or record['save'].get('job'),
            'meals': len(items), 'duplicates': dupes, 'last': max(times) if times else None,
            'following': record['id'] in following,
            'in_colony': (record['id'] in roster) if meta.get('roster_t') else None,
            'joined': joined[-1] if joined else None}


def followed_card(record):
    """What a card on the followed board needs: the newest of the live reading and the save for each part."""
    log, save = record['log'], record['save']
    base = summary(record)
    log_food = (log.get('food') or {}).get('items')
    use_log_food = log_food is not None and (not save.get('t') or (log.get('t') or '') >= save['t'])
    live_sat = log.get('saturation') is not None and (not save.get('t') or (log.get('saturation_t') or '') >= save['t'])
    changes = [e for e in record['events'] if e['kind'] != 'job']
    return dict(base,
                state=log.get('state'), job_state=log.get('job_state'), loaded=log.get('loaded'),
                reading_t=log.get('t'), reading_source=log.get('source'),
                foods=log_food if use_log_food else save.get('foods', []),
                saturation=log.get('saturation') if live_sat else save.get('saturation'),
                saturation_t=log.get('saturation_t') if live_sat else save.get('t'),
                last_event=changes[-1] if changes else None)


def roster_changes(limit=5):
    """Newest joins and departures across the colony."""
    changes = [{'t': e['t'], 'kind': e['kind'], 'id': r['id'], 'name': r['name']}
               for r in citizens.values() for e in r['events'] if e['kind'] in ('joined', 'left')]
    return sorted(changes, key=lambda c: c['t'], reverse=True)[:limit]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/':
            self.send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        elif url.path == '/api/state':
            query = parse_qs(url.query)
            with lock:
                selected = None
                if query.get('id') and query['id'][0].isdigit():
                    cid = int(query['id'][0])
                    if time.time() - watched.get(cid, 0) >= WATCH_SECONDS:
                        wake_poller.set()  # a citizen the poller is not reading yet: start now, not after the wait
                    watched[cid] = time.time()
                    record = citizens.get(cid)
                    selected = json.loads(json.dumps(record)) if record else None
                payload = {'meta': dict(meta, following=sorted(following), roster_changes=roster_changes(),
                                        new_citizen_minutes=NEW_CITIZEN_MINUTES), 'now': iso(datetime.now()),
                           'citizens': sorted((summary(r) for r in citizens.values()), key=lambda s: s['id']),
                           'followed': [followed_card(citizens[cid]) for cid in sorted(following) if cid in citizens],
                           'selected': selected}
            self.send(200, json.dumps(payload), 'application/json')
        else:
            self.send(404, 'Not found', 'text/plain')

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == '/api/hungry' and (query.get('id', [''])[0].isdigit() or query.get('all') == ['1']):
            ok, message = make_hungry(None if query.get('all') == ['1'] else [int(query['id'][0])])
            self.send(200 if ok else 409, json.dumps({'ok': ok, 'message': message}), 'application/json')
        elif url.path == '/api/locate' and query.get('id', [''])[0].isdigit():
            ok, message = locate_citizen(int(query['id'][0]))
            self.send(200 if ok else 409, json.dumps({'ok': ok, 'message': message, 'seconds': meta['glow_seconds']}), 'application/json')
        elif url.path == '/api/settings' and (query.get('ticks') or query.get('glow')):
            try:
                with lock:
                    if query.get('ticks'):
                        set_poll_ticks(int(query['ticks'][0]))
                    if query.get('glow'):
                        set_glow_seconds(int(query['glow'][0]))
                self.send(200, json.dumps({'poll_ticks': meta['poll_ticks'], 'poll_interval': meta['poll_interval'],
                                           'glow_seconds': meta['glow_seconds']}), 'application/json')
            except ValueError as error:
                self.send(400, json.dumps({'error': str(error)}), 'application/json')
        elif url.path == '/api/follow' and query.get('id', [''])[0].isdigit():
            with lock:
                set_following(int(query['id'][0]), query.get('on', ['1'])[0] == '1')
                body = json.dumps({'following': sorted(following)})
            self.send(200, body, 'application/json')
        else:
            self.send(404, 'Not found', 'text/plain')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--server-dir', type=Path,
                        help='dedicated server folder (server.properties with RCON enabled, ops.json)')
    parser.add_argument('--world-dir', type=Path,
                        help='world folder holding the colony save (default: the server\'s level-name folder)')
    parser.add_argument('--game-dir', type=Path,
                        help='game client folder whose logs/latest.log has /mc citizens info output typed in chat')
    parser.add_argument('--port', type=int, default=8765, help='port for the page on 127.0.0.1 (default: 8765)')
    parser.add_argument('--follow', default='', help='citizen IDs to read even with no page open, for this run only, e.g. 15,43')
    parser.add_argument('--journal', type=Path, default=HERE / 'data' / 'journal.jsonl', help='where everything the panel learns is kept')
    args = parser.parse_args()

    server = args.server_dir
    if server is not None and not (server / 'server.properties').is_file():
        parser.error(f'--server-dir: no server.properties in {server}')
    world = args.world_dir
    if world is None and server is not None:
        level = read_server_properties(server).get('level-name', 'world')
        world = server / level if (server / level).is_dir() else None
    if server is None and world is None and args.game_dir is None:
        parser.error('give at least one of --server-dir, --world-dir or --game-dir')

    journal_path = args.journal
    with lock:
        existed = replay_journal(journal_path)
        open_journal(journal_path)
        if not existed and args.game_dir is not None:
            imported = import_old_logs(args.game_dir / 'logs')
            print(f'New journal: imported readings from {imported} old log files', flush=True)
        for cid in (int(v) for v in args.follow.split(',') if v.strip().isdigit()):
            following.add(cid)  # command line follows last for this run only, the page button is remembered
    print(f'Journal {journal_path}: {sum(len(r["events"]) for r in citizens.values())} events, following {sorted(following) or "nobody"}', flush=True)

    if args.game_dir is not None:
        meta['log_path'] = str(args.game_dir / 'logs' / 'latest.log')
        threading.Thread(target=follow_log, args=(meta['log_path'],), daemon=True).start()
    if world is not None:
        save_patterns = [str(world / 'data' / 'minecolonies_colony_manager.dat'),
                         str(world / 'minecolonies' / '*' / '*' / 'colony*.dat')]
        threading.Thread(target=follow_save, args=(save_patterns,), daemon=True).start()
    if server is not None:
        global server_dir
        server_dir = str(server)
        meta['rcon_status'] = 'offline'
        threading.Thread(target=poll_rcon, args=(server_dir,), daemon=True).start()
        threading.Thread(target=keep_glows, args=(server_dir,), daemon=True).start()

    http_server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Citizen watch on http://127.0.0.1:{args.port}', flush=True)
    for label, value in (('server', server), ('world', world), ('game log', args.game_dir)):
        print(f'  {label}: {value if value is not None else "not used"}', flush=True)
    http_server.serve_forever()


if __name__ == '__main__':
    main()
