"""Web interface for RFID-FinishLynx middleware."""

import csv
import importlib.util
import io
import logging
import re
import os
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, Response
from flask_socketio import SocketIO

if TYPE_CHECKING:
    from .app import Application

logger = logging.getLogger(__name__)


# --- Athlete data from .ppl file ---
_athlete_data = {
    'by_bib': {},           # bib -> {last_name, first_name, school, gender}
    'by_school_gender': {}, # school -> {'M': [bibs], 'F': [bibs]}
    'loaded': False,
    'last_parsed_timestamp': None,  # mtime when file was last parsed
    'directory': None,      # directory containing lynx.ppl
}
_athlete_lock = threading.Lock()


def parse_ppl_file(filepath: str) -> dict:
    """Parse a FinishLynx .ppl file and return athlete data.

    .ppl format: bib,LastName,FirstName,School,,Sex
    (bib can be empty if athlete has no assigned number)

    Returns:
        Dict with 'by_bib' and 'by_school_gender' mappings.
    """
    by_bib = {}
    by_school_gender = {}

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith(';'):
                    continue

                parts = line.split(',')
                if len(parts) < 6:
                    continue

                bib = parts[0].strip()
                last_name = parts[1].strip()
                first_name = parts[2].strip()
                school = parts[3].strip()
                # parts[4] is usually empty
                gender = parts[5].strip().upper()

                if gender not in ('M', 'F'):
                    gender = ''

                # Store by bib (only if bib is assigned)
                if bib:
                    by_bib[bib] = {
                        'last_name': last_name,
                        'first_name': first_name,
                        'school': school,
                        'gender': gender,
                    }

                # Group by school and gender
                if school and gender:
                    if school not in by_school_gender:
                        by_school_gender[school] = {'M': [], 'F': []}
                    if bib:
                        by_school_gender[school][gender].append(bib)

        logger.info(f"Parsed .ppl file: {len(by_bib)} athletes with bibs, {len(by_school_gender)} schools")
        return {'by_bib': by_bib, 'by_school_gender': by_school_gender, 'loaded': True}

    except Exception as e:
        logger.error(f"Error parsing .ppl file: {e}")
        return {'by_bib': {}, 'by_school_gender': {}, 'loaded': False}


def _bare_name(name, ext: str) -> str:
    """A file name sent by the page: no folders, not absolute, and the
    expected extension. These routes wrote any content to any path the
    request named (2026-10-04 review); the folder stays the operator's pick."""
    name = str(name or "").strip()
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or os.path.isabs(name) or os.path.basename(name) != name):
        raise ValueError(f"not a plain file name: {name!r}")
    if not name.lower().endswith(ext):
        raise ValueError(f"only {ext} files can be saved here")
    return name


def start_list_dir(directory: str) -> str:
    """Where the meet's lynx.evt / lynx.ppl are: MeetPro writes them into
    the RFID subfolder of the Lynx results folder (see lif_merge.py); an
    older setup keeps them in the meet folder itself."""
    from .lif_merge import start_list_dir as _sld
    sub = "RFID"
    if _application is not None:
        sub = getattr(_application.config.races, "rfid_subdir", "") or sub
    return _sld(directory, sub)


def load_ppl_from_directory(directory: str) -> bool:
    """Load lynx.ppl for the meet folder (from its RFID subfolder when
    MeetPro writes there). `directory` stays the meet folder."""
    global _athlete_data
    import pathlib

    ppl_path = pathlib.Path(start_list_dir(directory)) / 'lynx.ppl'
    if not ppl_path.exists():
        logger.warning(f"No lynx.ppl file found in {directory}")
        return False

    data = parse_ppl_file(str(ppl_path))
    with _athlete_lock:
        _athlete_data = data
        _athlete_data['last_parsed_timestamp'] = ppl_path.stat().st_mtime
        _athlete_data['directory'] = directory

    return data['loaded']


def get_athlete_info(bib: str) -> dict:
    """Get athlete info by bib number.

    Meet mode (2026-10-03): the active meet answers first (panes, tag reads
    and bib validation all come through here); a bib the meet doesn't know
    falls back to the .ppl, so a legacy tag still gets a name. {} = unknown."""
    svc = getattr(_application, 'meet_service', None) if _application is not None else None
    if svc is not None:
        try:
            info = svc.athlete_lookup(bib)
        except Exception:
            logger.debug("meet athlete lookup failed", exc_info=True)
            info = None
        if info:
            return info
    with _athlete_lock:
        return _athlete_data['by_bib'].get(bib, {})


def get_athletes_by_school_gender(school: str, gender: str) -> list:
    """Get list of bibs for a school and gender."""
    with _athlete_lock:
        school_data = _athlete_data['by_school_gender'].get(school, {})
        return school_data.get(gender, [])


def get_meet_directory() -> Optional[str]:
    """Get the currently loaded meet directory.

    Returns:
        Path to meet directory, or None if not set.
    """
    with _athlete_lock:
        return _athlete_data.get('directory')


def get_participant_data() -> dict:
    """Get all participant data for LIF generation.

    Returns:
        Dict mapping bib -> {last_name, first_name, school, gender}
    """
    with _athlete_lock:
        return dict(_athlete_data.get('by_bib', {}))


def get_current_heat_info() -> Optional[dict]:
    """Get info for the currently selected heat.

    Returns:
        Dict with bibs, schools, gender, event_name, is_relay, etc.
        or None if no heat is selected.
    """
    with _event_data_lock:
        return _event_data.get('current_heat_info')


def has_current_event() -> bool:
    """True once the scoreboard feed has named an armed event/heat."""
    with _event_data_lock:
        return bool(_event_data.get('current_event'))


def has_evt_loaded() -> bool:
    """Check if any .evt data has been loaded.

    Returns:
        True if heats dict has any entries, False if empty.
    """
    with _event_data_lock:
        return len(_event_data.get('heats', {})) > 0


def get_event_info(event_num: int, heat_num: int) -> Optional[dict]:
    """Get event info for a specific event/heat.

    Args:
        event_num: Event number.
        heat_num: Heat number.

    Returns:
        Dict with event_name, gender, bibs, etc. or None if not found.
    """
    key = (str(event_num), str(heat_num))
    with _event_data_lock:
        heat_info = _event_data['heats'].get(key)
        if heat_info:
            return {
                'event_num': event_num,
                'heat_num': heat_num,
                'event_name': heat_info.get('event_name', ''),
                'gender': heat_info.get('gender', ''),
                'is_relay': heat_info.get('is_relay', False),
                'bibs': list(heat_info.get('bibs', set())),
                'bib_lanes': dict(heat_info.get('bib_lanes', {})),
                'schools': list(heat_info.get('schools', set())),
                'round': heat_info.get('round', 1),
                'distance': heat_info.get('distance', ''),
            }
    return None


def get_lane_for_bib(bib: str) -> Optional[str]:
    """Get the lane assignment for a bib in the current heat.

    Args:
        bib: Bib number.

    Returns:
        Lane number as string, or None if not found.
    """
    with _event_data_lock:
        heat_info = _event_data.get('current_heat_info')
        if not heat_info:
            return None

        bib_lanes = heat_info.get('bib_lanes', {})
        return bib_lanes.get(bib)


def get_current_sport() -> str:
    """Get the current sport type from .evt processing config.

    Returns:
        'track' or 'xc', defaults to 'track' if not set.
    """
    with _evt_monitor_lock:
        config = _evt_monitor_state.get('config')
        if config:
            return config.get('sport', 'track')
    return 'track'


def is_tdp_connected() -> bool:
    """Check if FinishLynx TDP is actively connected.

    Returns:
        True if TDP connection is active.
    """
    if _application and _application.scoreboard_listener:
        return _application.scoreboard_listener.tdp_connected
    return False


# --- Event/heat data and validation state ---
_event_data = {
    'heats': {},            # (event_num, heat_num) -> {bibs: set(), schools: set(), gender: 'M'/'F', is_relay: bool, event_name: str}
    'current_event': None,  # Currently selected (event_num, heat_num)
    'current_heat_info': None,  # Info for current heat (bibs, schools, gender, etc.)
}
_event_data_lock = threading.RLock()

_validation_state = {
    'pending_bibs': {},     # bib -> {reads: [], first_read_time: datetime, reason: str}
    'blacklisted_bibs': set(),  # Bibs rejected by user for current heat
    'accepted_bibs': set(),     # Bibs accepted by user (not in entries but approved)
}
_validation_lock = threading.Lock()

# --- Relay event data and state ---
_relay_data = {
    'heats': {},            # (event_num, heat_num) -> {lane -> {school, team_name}}
    'current_event': None,  # Currently selected (event_num, round, heat_num)
    'current_heat_data': None,  # Lane mappings for current heat
    'is_relay': False,      # Whether current event is a relay
}

_relay_state = {
    'team_legs': {},        # team_name -> {current_leg: int, bibs: {leg_num: bib}, lane: int}
    'bib_assignments': {},  # bib -> {team_name, leg_num}
    'retired_bibs': set(),  # Bibs whose leg is complete (ignore future reads)
    'school_team_order': {}, # school -> [team_names in order of first appearance]
    'base_reads_per_leg': 2,  # Base reads for middle legs (leg 1 gets -1, leg 4 gets +1)
    'total_legs': 4,        # Total number of legs in the relay (e.g., 4 for 4x800)
    'has_200m_split': False,  # Whether 200m split reader is configured
}
_relay_lock = threading.Lock()


def _determine_event_gender(event_name: str) -> str:
    """Determine gender from event name.

    Returns 'F' for female, 'M' for male, or '' if undetermined.
    """
    name_lower = event_name.lower()
    if 'girl' in name_lower or 'women' in name_lower or "women's" in name_lower:
        return 'F'
    elif 'boy' in name_lower or 'men' in name_lower or "men's" in name_lower:
        return 'M'
    return ''


def _is_relay_event(event_name: str) -> bool:
    """Check if event name indicates a relay."""
    name_lower = event_name.lower()
    return 'relay' in name_lower or '4 x' in name_lower or '4x' in name_lower


def evt_race_list(content: str) -> list:
    """Every event/round/heat header in a .evt as a list for the console's
    race picker: {event, round, heat, name, gender, is_relay, distance,
    entries, bibs}. `bibs` = the entered bibs (individual events), which
    become the race's start list when a pane is made from it. Order is
    file order (MeetPro writes the schedule order)."""
    races = []
    current = None
    for raw in content.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line or line.startswith(';'):
            continue
        parts = raw.split(',')
        if line[0].isdigit() and len(parts) >= 4:
            def _int(v, default):
                try:
                    return int(v.strip())
                except (ValueError, AttributeError):
                    return default
            name = parts[3].strip()
            distance = parts[9].strip() if len(parts) > 9 else ''
            current = {
                'event': _int(parts[0], 0), 'round': _int(parts[1], 1) or 1,
                'heat': _int(parts[2], 1) or 1, 'name': name,
                'gender': _determine_event_gender(name), 'is_relay': _is_relay_event(name),
                'distance': distance, 'entries': 0, 'bibs': [],
            }
            races.append(current)
        elif current is not None and raw.startswith(','):
            current['entries'] += 1
            # individual entry: ,bib,lane,Last,First,School (relays: ,,lane,School)
            bib = parts[1].strip() if len(parts) > 1 else ''
            if bib and bib.isdigit() and not current['is_relay']:
                current['bibs'].append(bib)
    return races


def create_missing_panes(mgr, evt_races: list) -> int:
    """One pending race pane per .evt event/round/heat that has none yet,
    in schedule order. Idempotent. Returns the number created."""
    with mgr._lock:
        existing = {(r.event, r.round, r.heat) for r in mgr.races.values()}
    made = 0
    for r in evt_races:
        if (r['event'], r['round'], r['heat']) in existing:
            continue
        key = mgr.create_race(name=r['name'], event=r['event'], gender=r.get('gender', ''),
                              round_=r['round'], heat=r['heat'], distance=str(r.get('distance', '')))
        if r.get('bibs'):
            mgr.set_entries(key, r['bibs'])
        existing.add((r['event'], r['round'], r['heat']))
        made += 1
    return made


def find_evt_file(directory: str) -> Optional[str]:
    """lynx.evt (any case) in the meet folder, else the only *.evt there."""
    if not directory or not os.path.isdir(directory):
        return None
    names = os.listdir(directory)
    for n in names:
        if n.lower() == 'lynx.evt':
            return n
    evts = [n for n in names if n.lower().endswith('.evt')]
    return evts[0] if len(evts) == 1 else None


def parse_all_heat_entries(content: str) -> dict:
    """Parse all event/heat entries from .evt content.

    For individual events: extracts bib numbers and lane assignments
    For relay events: extracts school names

    Returns:
        Dict mapping (event_num, heat_num) -> {
            'bibs': set of bib numbers (individual events),
            'bib_lanes': dict mapping bib -> lane (individual events),
            'schools': set of school names (relay events),
            'gender': 'M' or 'F',
            'is_relay': bool,
            'event_name': str,
        }
    """
    heats = {}
    current_event = None
    current_heat = None
    current_gender = ''
    current_is_relay = False
    current_event_name = ''

    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            continue

        parts = line.split(',')

        # Check if this is an event header line (starts with a number)
        if stripped and stripped[0].isdigit() and len(parts) >= 4:
            event_num = parts[0].strip()
            # round_num = parts[1].strip()  # Division/round
            heat_num = parts[2].strip()
            event_name = parts[3].strip()

            current_event = event_num
            current_heat = heat_num
            current_gender = _determine_event_gender(event_name)
            current_is_relay = _is_relay_event(event_name)
            current_event_name = event_name

            key = (event_num, heat_num)
            if key not in heats:
                heats[key] = {
                    'bibs': set(),
                    'bib_lanes': {},  # bib -> lane mapping
                    'schools': set(),
                    'gender': current_gender,
                    'is_relay': current_is_relay,
                    'event_name': current_event_name,
                }

        # Check if this is an entry line (starts with comma)
        elif current_event and line.startswith(','):
            key = (current_event, current_heat)

            if current_is_relay:
                # Relay format: ,,lane,School,,TeamName
                if len(parts) >= 4:
                    school = parts[3].strip()
                    if school:
                        heats[key]['schools'].add(school)
            else:
                # Individual format: ,bib,lane,LastName,FirstName,School
                if len(parts) >= 3:
                    bib = parts[1].strip()
                    lane = parts[2].strip()
                    if bib and bib.isdigit():
                        heats[key]['bibs'].add(bib)
                        if lane and lane.isdigit():
                            heats[key]['bib_lanes'][bib] = lane

    logger.info(f"Parsed heat entries: {len(heats)} heats, "
               f"{sum(len(h['bibs']) for h in heats.values())} individual entries, "
               f"{sum(len(h.get('bib_lanes', {})) for h in heats.values())} lane assignments, "
               f"{sum(len(h['schools']) for h in heats.values())} relay schools")
    return heats


def validate_bib_for_heat(bib: str) -> dict:
    """Validate a bib against the current heat's entries.

    Returns:
        Dict with:
        - 'status': 'valid', 'wrong_gender', 'pending', or 'blacklisted'
        - 'reason': explanation string
        - 'athlete_info': athlete data if available
    """
    result = {
        'status': 'valid',
        'reason': '',
        'athlete_info': None,
    }

    # Get athlete info
    athlete = get_athlete_info(bib)
    result['athlete_info'] = athlete

    with _validation_lock:
        # Check if blacklisted
        if bib in _validation_state['blacklisted_bibs']:
            result['status'] = 'blacklisted'
            result['reason'] = 'Rejected by user'
            return result

        # Check if already accepted
        if bib in _validation_state['accepted_bibs']:
            result['status'] = 'valid'
            result['reason'] = 'Accepted by user'
            return result

        # Check if pending
        if bib in _validation_state['pending_bibs']:
            result['status'] = 'pending'
            result['reason'] = 'Awaiting user decision'
            return result

    # Get current heat info
    heat_info = _event_data.get('current_heat_info')
    if not heat_info:
        # No heat selected - allow all
        return result

    event_gender = heat_info.get('gender', '')
    is_relay = heat_info.get('is_relay', False)

    # Check gender match
    if athlete and event_gender:
        athlete_gender = athlete.get('gender', '')
        if athlete_gender and athlete_gender != event_gender:
            result['status'] = 'wrong_gender'
            result['reason'] = f"Athlete is {athlete_gender}, event is {event_gender}"
            return result

    # Check if bib is in entries
    if is_relay:
        # For relays, check if athlete's school is in the heat
        if athlete:
            school = athlete.get('school', '')
            if school and school in heat_info.get('schools', set()):
                result['status'] = 'valid'
                result['reason'] = 'School in heat'
                return result
        # School not in heat or no athlete info
        result['status'] = 'pending'
        result['reason'] = 'School not in heat entries'
    else:
        # For individual events, check if bib is in entries
        if bib in heat_info.get('bibs', set()):
            result['status'] = 'valid'
            result['reason'] = 'Bib in heat entries'
            return result
        # Bib not in entries
        result['status'] = 'pending'
        result['reason'] = 'Bib not in heat entries'

    return result


def add_pending_bib(bib: str, read_data: dict, reason: str):
    """Add a bib to the pending list with its read data."""
    with _validation_lock:
        if bib not in _validation_state['pending_bibs']:
            _validation_state['pending_bibs'][bib] = {
                'reads': [],
                'first_read_time': read_data.get('timestamp'),
                'reason': reason,
            }
        _validation_state['pending_bibs'][bib]['reads'].append(read_data)


def accept_pending_bib(bib: str) -> list:
    """Accept a pending bib and return its held reads.

    Returns list of held read data to be processed.
    """
    with _validation_lock:
        _validation_state['accepted_bibs'].add(bib)
        pending_data = _validation_state['pending_bibs'].pop(bib, None)
        if pending_data:
            return pending_data.get('reads', [])
    return []


def reject_pending_bib(bib: str):
    """Reject a pending bib and add to blacklist."""
    with _validation_lock:
        _validation_state['blacklisted_bibs'].add(bib)
        _validation_state['pending_bibs'].pop(bib, None)


def reset_validation_state():
    """Reset validation state (for new heat/event)."""
    with _validation_lock:
        _validation_state['pending_bibs'] = {}
        _validation_state['blacklisted_bibs'] = set()
        _validation_state['accepted_bibs'] = set()


def set_current_heat(event_num: str, heat_num: str):
    """Set the current heat and load its validation data."""
    global _event_data

    key = (event_num, heat_num)
    with _event_data_lock:
        heat_info = _event_data['heats'].get(key)

        # Debug: log available keys to help diagnose matching issues
        available_keys = list(_event_data['heats'].keys())

        _event_data['current_event'] = key
        _event_data['current_heat_info'] = heat_info

    logger.info(f"set_current_heat: Looking for key {key}, loaded heats: {len(available_keys)}, keys: {available_keys[:5]}{'...' if len(available_keys) > 5 else ''}")

    # Reset validation state for new heat
    reset_validation_state()

    if heat_info:
        bib_lanes = heat_info.get('bib_lanes', {})
        logger.info(f"Set current heat: event {event_num} heat {heat_num}, "
                   f"gender={heat_info.get('gender')}, is_relay={heat_info.get('is_relay')}, "
                   f"bibs={len(heat_info.get('bibs', set()))}, bib_lanes={list(bib_lanes.keys())[:10]}")
    else:
        logger.warning(f"No heat info found for event {event_num} heat {heat_num} - bibs will not be filtered!")


def parse_relay_heats_from_evt(content: str) -> dict:
    """Parse relay event heats and lane assignments from .evt content.

    Returns:
        Dict mapping (event_num, heat_num) -> {lane -> {school, team_name}}
    """
    heats = {}
    current_event = None
    current_heat = None
    is_relay_event = False

    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            continue

        parts = line.split(',')

        # Check if this is an event header line (starts with a number)
        if stripped and stripped[0].isdigit() and len(parts) >= 4:
            event_num = parts[0].strip()
            # round_num = parts[1].strip()  # Division/round
            heat_num = parts[2].strip()
            event_name = parts[3].strip().lower()

            # Check if this is a relay event
            is_relay_event = '4 x 800' in event_name or '4x800' in event_name or \
                            '4 x 1600' in event_name or '4x1600' in event_name or \
                            'relay' in event_name

            if is_relay_event:
                current_event = event_num
                current_heat = heat_num
                key = (event_num, heat_num)
                if key not in heats:
                    heats[key] = {}
            else:
                current_event = None
                current_heat = None

        # Check if this is a team entry line (starts with ,,)
        elif is_relay_event and current_event and line.startswith(',,'):
            # Format: ,,Lane#,School,,TeamName
            if len(parts) >= 6:
                lane = parts[2].strip()
                school = parts[3].strip()
                team_name = parts[5].strip() if len(parts) > 5 else ''

                if lane and school:
                    key = (current_event, current_heat)
                    heats[key][lane] = {
                        'school': school,
                        'team_name': team_name or school,
                    }

    logger.info(f"Parsed relay heats: {len(heats)} heats found")
    return heats


def set_current_relay_event(event_num: str, heat_num: str, event_name: str):
    """Set the current relay event and load its heat data."""
    global _relay_data, _relay_state

    event_name_lower = event_name.lower()
    is_relay = '4 x 800' in event_name_lower or '4x800' in event_name_lower or \
               '4 x 1600' in event_name_lower or '4x1600' in event_name_lower or \
               'relay' in event_name_lower

    # Read monitor config before taking _relay_lock to keep a single global
    # lock order (_evt_monitor_lock is never taken while holding _relay_lock)
    with _evt_monitor_lock:
        config = _evt_monitor_state.get('config') or {}
        has_200m_split = config.get('splits') == '200m'

    with _relay_lock:
        _relay_data['current_event'] = (event_num, heat_num)
        _relay_data['is_relay'] = is_relay

        if is_relay:
            key = (event_num, heat_num)
            _relay_data['current_heat_data'] = _relay_data['heats'].get(key, {})
            _relay_state['has_200m_split'] = has_200m_split

            # Determine base reads per leg based on event type
            # Base = laps per runner (finish only) or laps * 2 (with 200m split)
            # Middle legs get base reads, leg 1 gets base-1, leg 4 gets base+1
            if '4x800' in event_name_lower or '4 x 800' in event_name_lower:
                laps_per_runner = 2  # 2 laps per runner for 4x800
                _relay_state['total_legs'] = 4
            elif '4x1600' in event_name_lower or '4 x 1600' in event_name_lower:
                laps_per_runner = 4  # 4 laps per runner for 4x1600
                _relay_state['total_legs'] = 4
            else:
                laps_per_runner = 2  # Default
                _relay_state['total_legs'] = 4

            # Calculate base reads: double if 200m split reader is present
            _relay_state['base_reads_per_leg'] = laps_per_runner * (2 if has_200m_split else 1)

            # Reset relay state for new heat
            _relay_state['team_legs'] = {}
            _relay_state['bib_assignments'] = {}
            _relay_state['retired_bibs'] = set()
            _relay_state['school_team_order'] = {}

            # Initialize team tracking for each lane
            for lane, data in _relay_data['current_heat_data'].items():
                team_name = data['team_name']
                school = data['school']

                _relay_state['team_legs'][team_name] = {
                    'current_leg': 1,
                    'bibs': {},  # leg_num -> bib
                    'lane': lane,
                    'school': school,
                    'read_count': {},  # bib -> number of reads recorded
                }

                # Track school -> team order
                if school not in _relay_state['school_team_order']:
                    _relay_state['school_team_order'][school] = []
                _relay_state['school_team_order'][school].append(team_name)

            # Sort teams alphabetically within each school
            for school in _relay_state['school_team_order']:
                _relay_state['school_team_order'][school].sort()

            logger.info(f"Set relay event: {event_name}, heat {heat_num}, {len(_relay_data['current_heat_data'])} teams, "
                       f"base_reads={_relay_state['base_reads_per_leg']}, 200m_split={has_200m_split}")
        else:
            _relay_data['current_heat_data'] = None
            logger.info(f"Set non-relay event: {event_name}")


def _get_max_reads_for_leg(leg: int) -> int:
    """Get the maximum allowed reads for a specific leg.

    Leg 1: base - 1 (no start line read)
    Middle legs: base
    Last leg: base + 1 (extra finish read)
    """
    base = _relay_state['base_reads_per_leg']
    total_legs = _relay_state['total_legs']

    if leg == 1:
        return base - 1
    elif leg == total_legs:
        return base + 1
    else:
        return base


def process_relay_read(bib: str, timestamp) -> dict:
    """Process a bib read for a relay event.

    Returns:
        Dict with 'lane' (to send to FinishLynx), 'team_name', 'leg', 'ignore', etc.
        If 'ignore' is True, the read should not be sent to FinishLynx.
    """
    global _relay_state

    result = {
        'lane': None,
        'team_name': None,
        'leg': None,
        'ignore': False,
        'reason': None,
        'athlete_info': None,
    }

    # Get athlete info
    athlete = get_athlete_info(bib)
    if not athlete:
        result['ignore'] = True
        result['reason'] = 'Unknown bib'
        return result

    result['athlete_info'] = athlete
    school = athlete['school']
    gender = athlete['gender']

    with _relay_lock:
        # Check if this is a relay event
        if not _relay_data['is_relay']:
            result['ignore'] = False
            result['lane'] = bib  # Non-relay: send bib as-is
            return result

        heat_data = _relay_data['current_heat_data']
        if not heat_data:
            result['ignore'] = True
            result['reason'] = 'No heat data loaded'
            return result

        # Check if bib is from a team in this heat
        teams_in_heat = [d['school'] for d in heat_data.values()]
        if school not in teams_in_heat:
            result['ignore'] = True
            result['reason'] = f'School {school} not in this heat'
            return result

        # Validate gender matches event (Women's vs Men's)
        # This is a simplification - in reality, check the event name
        # For now, we'll assume all bibs from teams in the heat are valid

        # Check if bib is retired
        if bib in _relay_state['retired_bibs']:
            result['ignore'] = True
            result['reason'] = 'Retired bib (leg complete)'
            return result

        # Check if bib is already assigned to a team
        if bib in _relay_state['bib_assignments']:
            assignment = _relay_state['bib_assignments'][bib]
            team_name = assignment['team_name']
            leg = assignment['leg']
            team_data = _relay_state['team_legs'].get(team_name, {})

            # Increment read count for this bib
            read_count = team_data.setdefault('read_count', {})
            read_count[bib] = read_count.get(bib, 0) + 1

            result['lane'] = team_data.get('lane')
            result['team_name'] = team_name
            result['leg'] = leg
            result['read_count'] = team_data['read_count'][bib]

            # Check if this runner has exceeded their allowed reads
            max_reads = _get_max_reads_for_leg(leg)
            if team_data['read_count'][bib] > max_reads:
                result['ignore'] = True
                result['reason'] = f'Leg {leg} complete ({max_reads} reads)'

            return result

        # NEW BIB - assign to next available team from this school
        school_teams = _relay_state['school_team_order'].get(school, [])
        if not school_teams:
            result['ignore'] = True
            result['reason'] = f'No teams found for school {school}'
            return result

        # Find the first team from this school that needs a new leg
        assigned_team = None
        for team_name in school_teams:
            team_data = _relay_state['team_legs'].get(team_name)
            if not team_data:
                continue

            current_leg = team_data['current_leg']
            if current_leg <= _relay_state['total_legs']:
                # This team needs a runner for the current leg
                # Check if this leg already has a bib assigned
                if current_leg not in team_data['bibs']:
                    assigned_team = team_name
                    break

        if not assigned_team:
            result['ignore'] = True
            result['reason'] = 'All teams from this school have runners assigned'
            return result

        team_data = _relay_state['team_legs'][assigned_team]
        current_leg = team_data['current_leg']

        # Retire the previous leg's bib (if any)
        if current_leg > 1:
            prev_leg = current_leg - 1
            prev_bib = team_data['bibs'].get(prev_leg)
            if prev_bib:
                _relay_state['retired_bibs'].add(prev_bib)
                logger.info(f"Retired bib {prev_bib} (leg {prev_leg} of {assigned_team})")

        # Assign this bib to the team
        team_data['bibs'][current_leg] = bib
        team_data['read_count'][bib] = 1
        _relay_state['bib_assignments'][bib] = {
            'team_name': assigned_team,
            'leg': current_leg,
        }

        # Advance to next leg for this team
        team_data['current_leg'] = current_leg + 1

        result['lane'] = team_data['lane']
        result['team_name'] = assigned_team
        result['leg'] = current_leg
        result['read_count'] = 1
        result['new_leg'] = True

        logger.info(f"Assigned bib {bib} to {assigned_team} leg {current_leg} (lane {team_data['lane']})")
        return result


def get_relay_display_info(bib: str) -> dict:
    """Get relay display info for a bib (team name, leg, etc.)."""
    with _relay_lock:
        if bib in _relay_state['bib_assignments']:
            assignment = _relay_state['bib_assignments'][bib]
            team_data = _relay_state['team_legs'].get(assignment['team_name'], {})
            return {
                'team_name': assignment['team_name'],
                'leg': assignment['leg'],
                'lane': team_data.get('lane'),
            }
    return {}


# --- .evt file auto-update monitoring state ---
_evt_monitor_state = {
    'last_applied_timestamp': None,  # datetime when templates were last successfully applied
    'monitored_file': None,          # filename of the .evt file being monitored
    'monitored_dir': None,           # directory containing the .evt file
    'config': None,                  # config dict (sport, splits) used for processing
    'content': None,                 # last known content of the file
    'monitor_thread': None,          # background monitoring thread
    'monitor_running': False,        # flag to control the monitor thread
    'retry_mode': False,             # True when retrying save every second
    'file_locked': False,            # True when file is locked by another process
    'races': None,                   # evt_race_list() of the file as last applied to the panes
    'last_sync': None,               # what the last .evt change did to the panes (console)
}
_evt_monitor_lock = threading.Lock()


def _apply_evt_to_panes(content: str, filename: str = '', fresh: bool = False) -> Optional[dict]:
    """Bring the race panes up to date with a (re)read .evt: start lists,
    and names/distances MeetPro changed (RaceManager.apply_evt). fresh =
    a different meet file than last time, so nothing to diff against."""
    races = evt_race_list(content)
    with _evt_monitor_lock:
        previous = None if fresh else _evt_monitor_state.get('races')
        _evt_monitor_state['races'] = races
    mgr = _race_manager()
    if mgr is None:
        return None
    summary = mgr.apply_evt(races, previous)
    if any(summary.values()):
        summary['at'] = datetime.now().isoformat(timespec='seconds')
        summary['file'] = filename
        with _evt_monitor_lock:
            _evt_monitor_state['last_sync'] = summary
        parts = []
        if summary['entries_added'] or summary['entries_removed']:
            parts.append(f"+{summary['entries_added']}/-{summary['entries_removed']} entries")
        if summary['renamed']:
            parts.append("renamed " + "; ".join(summary['renamed']))
        if summary['new_races']:
            parts.append(f"{len(summary['new_races'])} new race(s) without a pane: "
                         + ", ".join(summary['new_races']))
        logger.info(f".evt {filename} applied to the panes: " + ", ".join(parts))
        emit_races_update()
    return summary


def emit_evt_file_locked(locked: bool, message: str = ''):
    """Emit event to show/hide the file locked warning on the frontend."""
    socketio.emit('evt_file_locked', {'locked': locked, 'message': message})


def _get_evt_file_mtime(directory: str, filename: str) -> float:
    """Get the modification time of the .evt file."""
    import pathlib
    evt_path = pathlib.Path(start_list_dir(directory)) / filename
    if evt_path.exists():
        return evt_path.stat().st_mtime
    return 0


def _evt_monitor_loop():
    """Background loop that monitors the .evt and .ppl files for changes."""
    global _evt_monitor_state
    import pathlib

    while _evt_monitor_state['monitor_running']:
        try:
            # Check .evt file for changes
            with _evt_monitor_lock:
                if not _evt_monitor_state['last_applied_timestamp']:
                    # No file being monitored yet
                    pass
                elif _evt_monitor_state['monitored_file'] and _evt_monitor_state['monitored_dir']:
                    # Check file modification time
                    file_mtime = _get_evt_file_mtime(
                        _evt_monitor_state['monitored_dir'],
                        _evt_monitor_state['monitored_file']
                    )

                    if file_mtime > _evt_monitor_state['last_applied_timestamp']:
                        # File has been modified since we last applied templates
                        logger.info(f"Detected .evt file change, auto-reapplying templates")
                        _evt_monitor_state['retry_mode'] = True

            # Check .ppl file for changes
            with _athlete_lock:
                ppl_dir = _athlete_data.get('directory')
                ppl_timestamp = _athlete_data.get('last_parsed_timestamp')

            if ppl_dir and ppl_timestamp:
                ppl_path = pathlib.Path(start_list_dir(ppl_dir)) / 'lynx.ppl'
                if ppl_path.exists():
                    ppl_mtime = ppl_path.stat().st_mtime
                    if ppl_mtime > ppl_timestamp:
                        logger.info("Detected .ppl file change, reloading athlete data")
                        load_ppl_from_directory(ppl_dir)
                        socketio.emit('ppl_reloaded', {'message': 'Athlete data reloaded'})
                        emit_races_update()     # names/teams in the panes

            # If in retry mode, try to save more frequently (every 1 second)
            if _evt_monitor_state['retry_mode']:
                _attempt_evt_autosave()
                import time
                time.sleep(1)
            else:
                import time
                time.sleep(5)

        except Exception as e:
            logger.error(f"Error in .evt monitor loop: {e}")
            import time
            time.sleep(5)


def _attempt_evt_autosave():
    """Attempt to reprocess and save the .evt file."""
    global _evt_monitor_state
    import pathlib

    with _evt_monitor_lock:
        if not _evt_monitor_state['monitored_file'] or not _evt_monitor_state['monitored_dir']:
            _evt_monitor_state['retry_mode'] = False
            return

        directory = _evt_monitor_state['monitored_dir']
        filename = _evt_monitor_state['monitored_file']
        config = _evt_monitor_state['config']

    evt_path = pathlib.Path(start_list_dir(directory)) / filename

    try:
        # MeetPro may still be writing it: wait until it has sat for a
        # second and did not change while read (retry mode polls each second)
        import time
        before = evt_path.stat()
        if time.time() - before.st_mtime < 1.0:
            return
        # read-only reloads tolerate a non-UTF-8 name; a rewrite must not
        with open(evt_path, 'r', encoding='utf-8',
                  errors='replace' if config is None else 'strict') as f:
            current_content = f.read()
        after = evt_path.stat()
        if (after.st_mtime, after.st_size) != (before.st_mtime, before.st_size):
            return

        if config is None:
            # File was loaded without a processing config (plain load):
            # reload data only, never rewrite the meet file with defaults.
            modified_content = current_content
            templates_used = []
            events_modified = 0
            events_with_templates = 0
        else:
            # Process the file with templates
            sport = config.get('sport', 'track')
            splits = config.get('splits', 'finish')
            track_splits = splits if sport == 'track' else 'finish'

            modified_content, templates_used, events_modified, events_with_templates = process_evt_file_with_templates(
                current_content, sport, track_splits
            )

            # Copy templates if needed
            if templates_used:
                copy_templates_to_directory(templates_used, directory)

            # Try to save the file
            with open(evt_path, 'w', encoding='utf-8') as f:
                f.write(modified_content)

        # Success! Update state
        with _evt_monitor_lock:
            _evt_monitor_state['last_applied_timestamp'] = evt_path.stat().st_mtime
            _evt_monitor_state['content'] = modified_content
            _evt_monitor_state['retry_mode'] = False
            _evt_monitor_state['file_locked'] = False

        if config is None:
            logger.info("Reloaded .evt data (no processing config, file left untouched)")
        else:
            logger.info(f"Auto-updated .evt file: {events_modified} events processed")
        emit_evt_file_locked(False)

        # Reparse relay heats from the updated content
        heats = parse_relay_heats_from_evt(modified_content)
        with _relay_lock:
            _relay_data['heats'] = heats
        if heats:
            logger.info(f"Reparsed relay heats: {len(heats)} heats found")
            # Build relay events list for frontend
            relay_events = []
            for (event_num, heat_num), lane_data in heats.items():
                teams = [f"{d['team_name']} (Lane {lane})" for lane, d in lane_data.items()]
                relay_events.append({
                    'event_num': event_num,
                    'heat_num': heat_num,
                    'teams': teams,
                    'lane_count': len(lane_data),
                })
            socketio.emit('relay_heats_updated', {
                'heats_found': len(heats),
                'relay_events': relay_events,
            })

        # Reparse all event entries (individual events with bib/lane mappings)
        all_entries = parse_all_heat_entries(modified_content)
        with _event_data_lock:
            _event_data['heats'] = all_entries

            # Refresh current heat info if one is selected
            current_event = _event_data.get('current_event')
            if current_event and current_event in all_entries:
                _event_data['current_heat_info'] = all_entries[current_event]
                logger.debug(f"Refreshed current heat info for {current_event}")
        logger.info(f"Reparsed event entries: {len(all_entries)} heats, "
                   f"{sum(len(h.get('bib_lanes', {})) for h in all_entries.values())} lane assignments")

        # ...and the race panes made from it: start lists, names, distances
        _apply_evt_to_panes(modified_content, filename)

        # Emit update to frontend
        if config is not None:
            socketio.emit('evt_auto_updated', {
                'success': True,
                'events_modified': events_modified,
                'templates_used': templates_used,
                'events_with_templates': events_with_templates,
            })

    except PermissionError as e:
        # File is locked by another process (likely Finishlynx)
        with _evt_monitor_lock:
            if not _evt_monitor_state['file_locked']:
                _evt_monitor_state['file_locked'] = True
                logger.warning(f"Cannot save .evt file - file is in use: {e}")
                emit_evt_file_locked(True, "Event file updated. Restart Finishlynx.")

    except FileNotFoundError as e:
        # mid-replace by MeetPro: picked up again once it has a new mtime
        logger.warning(f"EVT file not found: {evt_path} - {e}")
        with _evt_monitor_lock:
            _evt_monitor_state['retry_mode'] = False

    except OSError as e:
        # Could be a Win32 sharing violation or similar
        if 'being used by another process' in str(e).lower() or e.errno == 13:
            with _evt_monitor_lock:
                if not _evt_monitor_state['file_locked']:
                    _evt_monitor_state['file_locked'] = True
                    logger.warning(f"Cannot save .evt file - file is in use: {e}")
                    emit_evt_file_locked(True, "Event file updated. Restart Finishlynx.")
        else:
            logger.error(f"OS error saving .evt file: {e}", exc_info=True)
            with _evt_monitor_lock:
                _evt_monitor_state['retry_mode'] = False

    except UnicodeDecodeError as e:
        logger.error(f"EVT file encoding error (not UTF-8?): {e}")
        with _evt_monitor_lock:
            _evt_monitor_state['retry_mode'] = False

    except Exception as e:
        logger.error(f"Error auto-saving .evt file: {e}", exc_info=True)
        logger.error(f"EVT autosave details: file={evt_path}, config={config}")
        with _evt_monitor_lock:
            _evt_monitor_state['retry_mode'] = False


def start_evt_monitor():
    """Start the background .evt file monitor if not already running."""
    global _evt_monitor_state

    with _evt_monitor_lock:
        if _evt_monitor_state['monitor_thread'] is not None and _evt_monitor_state['monitor_running']:
            return  # Already running

        _evt_monitor_state['monitor_running'] = True
        _evt_monitor_state['monitor_thread'] = threading.Thread(
            target=_evt_monitor_loop,
            daemon=True,
            name='evt-monitor'
        )
        _evt_monitor_state['monitor_thread'].start()
        logger.info("Started .evt file monitor")


def stop_evt_monitor():
    """Stop the background .evt file monitor."""
    global _evt_monitor_state

    with _evt_monitor_lock:
        thread = _evt_monitor_state['monitor_thread']
        _evt_monitor_state['monitor_running'] = False
        _evt_monitor_state['monitor_thread'] = None

    if thread and thread is not threading.current_thread():
        thread.join(timeout=7.0)


class WebLogHandler(logging.Handler):
    """Captures log messages for the web UI error viewer."""

    def __init__(self, max_entries=500):
        super().__init__()
        self._records = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def emit(self, record):
        entry = {
            'timestamp': datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            'level': record.levelname,
            'logger': record.name,
            'message': self.format(record),
        }
        with self._lock:
            self._records.append(entry)

    def get_logs(self, level_filter=None):
        with self._lock:
            if level_filter:
                levels = [l.upper() for l in level_filter]
                return [r for r in self._records if r['level'] in levels]
            return list(self._records)

    def clear(self):
        with self._lock:
            self._records.clear()


# Global log handler instance - captures all levels for the web UI viewer
_log_handler = WebLogHandler(max_entries=2000)
_log_handler.setLevel(logging.DEBUG)
_log_handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))


def install_log_handler():
    """Install the web log handler on the root logger."""
    root = logging.getLogger()
    root.addHandler(_log_handler)


# Flask app setup
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
static_dir = os.path.join(os.path.dirname(__file__), 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.config['SECRET_KEY'] = 'rfid-finishlynx-secret'
# Same-origin only (the default): a page from another site must not drive
# the console over its socket (2026-10-04 review)
socketio = SocketIO(app, async_mode='threading')


@app.before_request
def _same_origin_only():
    """Refuse a change sent by another site's page in this browser: with no
    login, the browser's Origin header is what tells the console's own page
    from a malicious one. Tools without a browser send no Origin."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    origin = request.headers.get('Origin')
    if origin and (origin == 'null' or urlsplit(origin).netloc != request.host):
        logger.warning(f"Refused {request.method} {request.path} from {origin}")
        return jsonify({'error': 'cross-site request refused'}), 403
    return None

# Reference to main application (set during startup)
_application: Optional['Application'] = None


# --- Operator actions in the log (2026-10-01) ---
# Every button that changes something is a POST to /api/...: one "UI:" line
# each, with the pane or read it touched, so a log sent for support shows
# what the operator did (most actions logged nothing before).
# "email" too (2026-10-03): the MileSplit login posts {email, password}.
_UI_HIDDEN = {"token", "logs_token", "password", "email"}


@app.before_request
def _log_operator_action():
    if request.method != "POST" or not request.path.startswith("/api/"):
        return
    try:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            body = {k: ("(hidden)" if k in _UI_HIDDEN else v) for k, v in body.items()}
        import json
        detail = json.dumps(body, default=str)[:300] if body else ""
        args = request.view_args or {}
        mgr = _race_manager()
        who = ""
        if mgr is not None and args.get("key") in mgr.races:
            who = f" [{mgr.races[args['key']].name}]"
        if mgr is not None and args.get("read_id") in mgr.reads:
            r = mgr.reads[args["read_id"]]
            who += f" [read {r.id}: bib {r.bib} at {r.point} {r.eff_ts.strftime('%H:%M:%S.%f')[:-3]}]"
        logger.info(f"UI: {request.path}{who} {detail}".rstrip())
    except Exception:
        logger.debug("UI action log failed", exc_info=True)


@app.after_request
def _log_operator_failure(response):
    if request.method == "POST" and request.path.startswith("/api/") and response.status_code >= 400:
        logger.warning(f"UI: {request.path} -> HTTP {response.status_code}")
    return response


def _autoload_meet(application):
    """Load athlete data from the persisted meet folder at boot."""
    directory = application.config.event_directory
    if directory and os.path.isdir(directory):
        if load_ppl_from_directory(directory):
            with _athlete_lock:
                count = len(_athlete_data['by_bib'])
            logger.info(f"Meet folder restored: {count} athletes from {directory}")
        # the .evt too, so its changes keep reaching the panes after a restart
        if getattr(application, 'race_manager', None) is not None:
            _autoload_evt(directory)


def set_application(application: 'Application'):
    """Set the main application reference."""
    global _application
    _application = application
    try:
        _autoload_meet(application)
    except Exception:
        logger.exception("Meet folder autoload failed")
    # After a self-update: once this version has run for 90 s it is kept
    # (deploy/boot_guard.py rolls back one that keeps failing to start).
    # Stations update from tools/build_station.py packages, not GitHub.
    if application.config.mode == "finish_line":
        from . import updater
        t = threading.Timer(90.0, lambda: updater.confirm())
        t.daemon = True
        t.start()


def emit_tag_read(bib: str, timestamp: datetime, reader_id: str,
                  lap_number: Optional[int] = None, position: Optional[int] = None,
                  extra_data: Optional[dict] = None):
    """Emit a tag read event to connected web clients."""
    data = {
        'bib': bib,
        'timestamp': timestamp.strftime('%H:%M:%S.') + f'{timestamp.microsecond // 1000:03d}',
        'reader_id': reader_id,
        'full_timestamp': timestamp.isoformat(),
    }
    if lap_number is not None:
        data['lap_number'] = lap_number
        data['total_laps'] = _application.config.timing.total_laps if _application else 1
    if position is not None:
        data['position'] = position

    # Add athlete info from .ppl file
    athlete = get_athlete_info(bib)
    if athlete:
        data['athlete'] = {
            'first_name': athlete.get('first_name', ''),
            'last_name': athlete.get('last_name', ''),
            'school': athlete.get('school', ''),
            'gender': athlete.get('gender', ''),
            'name': f"{athlete.get('first_name', '')} {athlete.get('last_name', '')}".strip(),
        }

    # Add relay info if present
    if extra_data:
        if extra_data.get('split_point'):
            # reader stands at several split points; this read's pass number
            # made it this point (see races.reader_points)
            data['point'] = extra_data['split_point']
        if extra_data.get('team_name'):
            data['relay'] = {
                'team_name': extra_data.get('team_name'),
                'leg': extra_data.get('leg'),
                'lane': extra_data.get('lane'),
            }
        if extra_data.get('athlete_info'):
            # Relay processing may have already looked up athlete
            athlete_info = extra_data['athlete_info']
            data['athlete'] = {
                'first_name': athlete_info.get('first_name', ''),
                'last_name': athlete_info.get('last_name', ''),
                'school': athlete_info.get('school', ''),
                'name': f"{athlete_info.get('first_name', '')} {athlete_info.get('last_name', '')}".strip(),
            }

    socketio.emit('tag_read', data)


def process_read_for_finishlynx(bib: str, timestamp) -> tuple:
    """Process a bib read and determine what to send to FinishLynx.

    This is the callback for validation and relay mode processing.

    Returns:
        Tuple of (identifier, should_send, extra_data)
        extra_data may include 'validation_status' for UI display
    """
    # First, validate the bib against current heat entries
    validation = validate_bib_for_heat(bib)
    validation_status = validation['status']
    athlete = validation.get('athlete_info')

    # Handle validation results
    if validation_status == 'wrong_gender':
        # Auto-reject wrong gender - don't send, don't show in log
        logger.info(f"Rejected bib {bib}: {validation['reason']}")
        return (None, False, {
            'reason': validation['reason'],
            'validation_status': 'rejected',
            'show_in_log': False,  # Don't even show in UI
        })

    if validation_status == 'blacklisted':
        # User rejected this bib - ignore silently
        return (None, False, {
            'reason': validation['reason'],
            'validation_status': 'blacklisted',
            'show_in_log': False,
        })

    if validation_status == 'pending':
        # Pending bibs only apply for track meets when TDP is not connected
        # For XC or when TDP is connected, pass the read through
        sport = get_current_sport()
        tdp_connected = is_tdp_connected()

        if sport == 'xc' or tdp_connected:
            # Skip pending - just pass through
            logger.debug(f"Skipping pending for bib {bib}: sport={sport}, tdp_connected={tdp_connected}")
            return (bib, True, {
                'athlete_info': athlete,
                'validation_status': 'passed',  # Not validated but allowed
            })

        # Track meet with no TDP - hold this read and show in UI for user decision
        read_data = {
            'bib': bib,
            'timestamp': timestamp,
            'athlete_info': athlete,
        }
        add_pending_bib(bib, read_data, validation['reason'])

        # Emit pending event to UI
        socketio.emit('bib_pending', {
            'bib': bib,
            'reason': validation['reason'],
            'athlete_info': {
                'first_name': athlete.get('first_name', '') if athlete else '',
                'last_name': athlete.get('last_name', '') if athlete else '',
                'school': athlete.get('school', '') if athlete else '',
            } if athlete else None,
        })

        return (None, False, {
            'reason': validation['reason'],
            'validation_status': 'pending',
            'show_in_log': True,  # Show in UI with yellow background
            'athlete_info': athlete,
        })

    # Bib is valid - proceed with normal processing
    with _relay_lock:
        if not _relay_data['is_relay']:
            # Not in relay mode - send bib directly
            return (bib, True, {
                'athlete_info': athlete,
                'validation_status': 'valid',
            })

    # Relay mode - use relay processing logic
    result = process_relay_read(bib, timestamp)

    if result['ignore']:
        return (None, False, {
            'reason': result['reason'],
            'validation_status': 'valid',  # Validation passed, relay logic rejected
        })

    # Return lane number for relays
    return (
        result['lane'],
        True,
        {
            'team_name': result.get('team_name'),
            'leg': result.get('leg'),
            'lane': result.get('lane'),
            'athlete_info': result.get('athlete_info'),
            'new_leg': result.get('new_leg', False),
            'validation_status': 'valid',
        }
    )


def emit_remote_unit_update(unit_data: dict):
    """Emit remote unit status change to web clients."""
    socketio.emit('remote_unit_update', unit_data)


def emit_reads_cleared():
    """Emit reads cleared event to web clients (auto-clears read log on event change)."""
    socketio.emit('reads_cleared', {'message': 'Reads cleared for new event'})


def emit_status_update():
    """Emit current status to connected web clients."""
    if _application:
        socketio.emit('status_update', _application.get_status())


@app.route('/')
def index():
    """Main console. Finish line gets the unified pane console; remote
    stations keep the classic dashboard (it is reader-centric)."""
    if _application and _application.race_manager:
        return render_template('console.html')
    return render_template('index.html')


def _console_page():
    """console.html, or the dashboard in the station package (no console)."""
    if not os.path.exists(os.path.join(app.root_path, app.template_folder, 'console.html')):
        return render_template('index.html')
    return render_template('console.html')


@app.route('/console')
def console():
    """The finish-line pane console, addressable directly so the legacy
    dashboard can always link back to it (the root route falls back to
    the legacy page when no race manager exists)."""
    return _console_page()


@app.route('/classic')
def classic():
    """The legacy dashboard (reader config, serial tools, time sync...)."""
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Get current system status."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(_application.get_status())


@app.route('/api/setup_check')
def api_setup_check():
    """Is this PC ready for a meet? Rows for the Console chip's Setup check
    (setup_check.py): reader, clocks, FinishLynx, folders, camera start."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    from .setup_check import run
    try:
        return jsonify({'rows': run(_application)})
    except Exception as e:
        logger.exception("Setup check failed")
        return jsonify({'rows': [], 'error': str(e)}), 500


def _support_key_set(config) -> bool:
    """A Send-logs key is there (typed in, or support.key from the install).
    The station package has no support.py: never."""
    try:
        from .support import effective_token
    except ImportError:
        return False
    return bool(effective_token(config))


@app.route('/api/config', methods=['GET'])
def api_get_config():
    """Get current configuration."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    config = _application.config
    return jsonify({
        'mode': config.mode,
        'display_name': config.display_name,
        'header_color': config.header_color,
        'remote_readers': [
            {'name': r.name, 'host': r.host, 'web_port': r.web_port}
            for r in config.remote_readers
        ],
        'reader': {
            'host': config.reader.host,
            'port': config.reader.port,
            'antennas': config.reader.antennas,
            'power_dbm': config.reader.power_dbm,
            'type': config.reader.type,
        },
        'timing': {
            'dedup_window_sec': config.timing.dedup_window_sec,
            'lap_mode': config.timing.lap_mode,
            'total_laps': config.timing.total_laps,
        },
        'finishlynx': {
            'host': config.finishlynx.host,
            'port': config.finishlynx.port,
            'sync_mode': config.finishlynx.sync_mode,
            'feed': getattr(config.finishlynx, 'feed', 'finish'),
        },
        'scoreboard': {
            'enabled': config.scoreboard.enabled,
            'host': config.scoreboard.host,
            'port': config.scoreboard.port,
        },
        'lora': {
            'enabled': config.lora.enabled,
            'port': config.lora.port,
            'baud': config.lora.baud,
            'reader_id': config.lora.reader_id,
            'pre_registered_units': config.lora.pre_registered_units,
        },
        'led_clock': {
            'enabled': config.led_clock.enabled,
            'hour_format': config.led_clock.hour_format,
        },
        'races': {
            'point_labels': dict(config.races.point_labels),
            'finish_readers': list(config.races.finish_readers),
            'reader_points': {k: list(v) for k, v in config.races.reader_points.items()},
            'pass_window_sec': config.races.pass_window_sec,
        },
        'event_directory': config.event_directory,
        'evt_file': config.evt_file,
        # the support key itself never leaves the PC
        'support': {
            'site_name': config.support.site_name,
            'token_set': _support_key_set(config),
            'update_repo': config.support.update_repo,
        },
    })


@app.route('/api/config', methods=['POST'])
def api_set_config():
    """Update configuration."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    config = _application.config
    restart_needed = False

    reader_restart_needed = False

    # Update reader settings
    if 'reader' in data:
        r = data['reader']
        if 'type' in r and r['type'] != config.reader.type:
            config.reader.type = r['type']
            reader_restart_needed = True
        if 'host' in r and r['host'] != config.reader.host:
            config.reader.host = r['host']
            restart_needed = True
        if 'port' in r:
            config.reader.port = int(r['port'])
        if 'power_dbm' in r:
            new_power = float(r['power_dbm'])
            if new_power != config.reader.power_dbm:
                config.reader.power_dbm = new_power
                reader_restart_needed = True
        if 'antennas' in r:
            new_antennas = [int(a) for a in r['antennas']]
            if new_antennas != config.reader.antennas:
                config.reader.antennas = new_antennas
                reader_restart_needed = True

    # Update timing settings
    # Note: dedup_window_sec is hardcoded to 12 seconds and not configurable via UI
    # lap_mode and total_laps are now managed through the Race Setup / .evt file section
    if 'timing' in data:
        t = data['timing']
        if 'lap_mode' in t:
            config.timing.lap_mode = bool(t['lap_mode'])
        if 'total_laps' in t:
            config.timing.total_laps = max(1, int(t['total_laps']))

    finishlynx_reconnect_needed = False

    # Update FinishLynx settings
    if 'finishlynx' in data:
        f = data['finishlynx']
        if 'host' in f and f['host'] != config.finishlynx.host:
            config.finishlynx.host = f['host']
            restart_needed = True
        if 'port' in f:
            new_port = int(f['port'])
            if new_port != config.finishlynx.port:
                config.finishlynx.port = new_port
                finishlynx_reconnect_needed = True
        if 'sync_mode' in f:
            if f['sync_mode'] != config.finishlynx.sync_mode:
                config.finishlynx.sync_mode = f['sync_mode']
                finishlynx_reconnect_needed = True
        if 'feed' in f:
            feed = str(f['feed'] or '').strip().lower()
            if feed not in ('finish', 'all'):
                return jsonify({'success': False, 'error': "feed must be 'finish' or 'all'"}), 400
            config.finishlynx.feed = feed

    scoreboard_restart_needed = False
    # Update Scoreboard Listener settings (receives event/heat info from FinishLynx)
    # Support both new "scoreboard" key and old "finishlynx_listener" key for backwards compatibility
    scoreboard_data = data.get('scoreboard') or data.get('finishlynx_listener')
    if scoreboard_data:
        if 'enabled' in scoreboard_data:
            config.scoreboard.enabled = bool(scoreboard_data['enabled'])
        if 'host' in scoreboard_data and scoreboard_data['host'] != config.scoreboard.host:
            config.scoreboard.host = scoreboard_data['host']
            scoreboard_restart_needed = True  # Listener needs restart if host changes
        if 'port' in scoreboard_data:
            new_port = int(scoreboard_data['port'])
            if new_port != config.scoreboard.port:
                config.scoreboard.port = new_port
                scoreboard_restart_needed = True  # Listener needs restart if port changes

    # LED clock display: time-of-day in 12- or 24-hour form
    if 'led_clock' in data and 'hour_format' in (data['led_clock'] or {}):
        fmt = str(data['led_clock']['hour_format']).strip()
        if fmt not in ('12', '24'):
            return jsonify({'success': False, 'error': "hour_format must be '12' or '24'"}), 400
        _application.set_clock_hour_format(fmt)

    # Update LoRa settings
    if 'lora' in data:
        l = data['lora']
        if 'enabled' in l:
            config.lora.enabled = bool(l['enabled'])
        if 'port' in l:
            config.lora.port = l['port']
        if 'baud' in l:
            try:
                config.lora.baud = int(l['baud'])
            except (TypeError, ValueError):
                pass
        if 'reader_id' in l:
            config.lora.reader_id = l['reader_id']

    # Race panes: a reader that stands at more than one split point (one Pi
    # where Mile 1 and Mile 2 share a spot) - {reader_id: [S1, S2]}. The
    # whole map is replaced; an empty map means every reader is its own point.
    if 'races' in data and isinstance(data['races'], dict):
        rc = data['races']
        if 'reader_points' in rc:
            rp = rc['reader_points']
            if not isinstance(rp, dict):
                return jsonify({'success': False,
                                'error': 'reader_points must map a reader id to a list of points'}), 400
            from .races import RaceManager
            clean = {}
            for reader, pts in rp.items():
                pts = RaceManager.normalize_points(pts)
                if str(reader).strip() and pts:
                    clean[str(reader).strip()] = pts
            chute = config.races.chute_reader
            if any(chute in pts and pts != [chute] for pts in clean.values()):
                return jsonify({'success': False,
                                'error': 'Check-in (chute) cannot be combined with split points'}), 400
            known = (set(config.races.point_distances) | set(config.races.point_labels)
                     | set(config.races.finish_readers) | {RaceManager.SKIP, RaceManager.OFF, chute})
            bad = sorted({p for pts in clean.values() for p in pts if p not in known})
            if any(all(p == RaceManager.SKIP for p in pts) for pts in clean.values()):
                return jsonify({'success': False,
                                'error': 'a reader needs at least one real split point'}), 400
            if any(RaceManager.OFF in pts and pts != [RaceManager.OFF] for pts in clean.values()):
                return jsonify({'success': False,
                                'error': '"off" (not in use) cannot be combined with split points'}), 400
            if bad:
                return jsonify({'success': False,
                                'error': f"unknown split point(s): {', '.join(bad)}"}), 400
            config.races.reader_points = clean
        if 'pass_window_sec' in rc:
            try:
                window = float(rc['pass_window_sec'])
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'pass_window_sec must be a number'}), 400
            if not 0 <= window <= 3600:
                return jsonify({'success': False, 'error': 'pass_window_sec must be 0-3600 s'}), 400
            config.races.pass_window_sec = window
        mgr = getattr(_application, 'race_manager', None)
        if mgr and ('reader_points' in rc or 'pass_window_sec' in rc):
            mgr.set_reader_points(config.races.reader_points, config.races.pass_window_sec)

    # Save to config file
    _application.save_config()

    # Apply power/antenna changes to running reader
    reader_restarted = False
    if reader_restart_needed and _application.is_running:
        reader_restarted = _application.restart_reader()

    # Reconnect FinishLynx if sync_mode or port changed
    finishlynx_reconnected = False
    if finishlynx_reconnect_needed and _application.finishlynx:
        from .finishlynx import FinishLynxOutput
        _application.finishlynx.disconnect()
        _application.finishlynx = FinishLynxOutput(config.finishlynx)
        finishlynx_reconnected = _application.finishlynx.connect()

    # Restart Scoreboard Listener if its settings changed
    scoreboard_restarted = False
    if scoreboard_restart_needed and _application.is_running:
        scoreboard_restarted = _application.restart_scoreboard_listener()

    messages = ['Configuration saved']
    if reader_restarted:
        messages.append('reader restarted with new settings')
    if finishlynx_reconnected:
        messages.append('FinishLynx reconnected with new settings')
    if scoreboard_restarted:
        messages.append('Scoreboard Listener restarted with new settings')
    if restart_needed:
        messages.append('restart required for IP changes')

    return jsonify({
        'success': True,
        'restart_needed': restart_needed,
        'reader_restarted': reader_restarted,
        'scoreboard_restarted': scoreboard_restarted,
        'message': ' - '.join(messages),
    })


@app.route('/api/reads')
def api_get_reads():
    """Get all tag reads. Supports ?sort=asc|desc and ?detail=true."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    reads = _application.get_reads()
    sort_order = request.args.get('sort', 'desc')
    if sort_order == 'asc':
        reads = list(reversed(reads))
    limit = request.args.get('limit', type=int)
    if limit:
        reads = reads[:limit]
    # ?detail=true: name / school / gender from the loaded .ppl, so the
    # console's "Add athlete" list can show who each read was.
    if request.args.get('detail', '').lower() in ('1', 'true', 'yes'):
        reads = [dict(r, athlete=athlete_summary(r.get('bib'))) for r in reads]

    return jsonify(reads)


def athlete_summary(bib) -> Optional[dict]:
    """{first_name, last_name, school, gender, name} for a bib, or None."""
    a = get_athlete_info(str(bib)) if bib is not None else None
    if not a:
        return None
    return {
        'first_name': a.get('first_name', ''),
        'last_name': a.get('last_name', ''),
        'school': a.get('school', ''),
        'gender': a.get('gender', ''),
        'name': f"{a.get('first_name', '')} {a.get('last_name', '')}".strip(),
    }


@app.route('/api/reads/<bib>')
def api_get_bib_detail(bib):
    """Get full lap detail for a specific bib."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    detail = _application.get_bib_detail(bib)
    if detail is None:
        return jsonify({'error': 'Bib not found'}), 404
    return jsonify(detail)


@app.route('/api/reads/all-laps')
def api_get_all_laps():
    """Get all bibs' lap data for cross-reference (used for time interpolation)."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    all_laps = _application.get_all_laps_summary()
    return jsonify(all_laps)


@app.route('/api/reads/<bib>/adjust', methods=['POST'])
def api_adjust_bib(bib):
    """Adjust a bib's lap count.

    Body for add: {"action": "add", "after_lap": 0, "time": "HH:MM:SS.S"}
    Body for remove: {"action": "remove", "lap_number": 1}
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    action = data.get('action', '')
    if action not in ('add', 'remove'):
        return jsonify({'error': 'Action must be "add" or "remove"'}), 400

    lap_number = data.get('lap_number')
    after_lap = data.get('after_lap')
    time_str = data.get('time')

    result = _application.adjust_bib_lap(
        bib, action,
        lap_number=int(lap_number) if lap_number is not None else None,
        after_lap=int(after_lap) if after_lap is not None else None,
        time_str=time_str
    )
    if result is None:
        return jsonify({'error': 'Cannot adjust (invalid lap or no laps to remove)'}), 400
    return jsonify({'success': True, 'detail': result})


@app.route('/api/reads/<bib>/edit-time', methods=['POST'])
def api_edit_bib_time(bib):
    """Edit the TOD time for a bib's lap.

    Body: {"time": "HH:MM:SS.S", "lap_number": 1}  (lap_number optional, defaults to last)
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    time_str = data.get('time', '')
    lap_number = data.get('lap_number')

    if not time_str:
        return jsonify({'error': 'time is required'}), 400

    result = _application.edit_bib_time(
        bib, time_str,
        lap_number=int(lap_number) if lap_number is not None else None
    )
    if result is None:
        return jsonify({'error': 'Invalid time format or bib not found'}), 400
    return jsonify({'success': True, 'detail': result})


@app.route('/api/reads/export')
def api_export_reads():
    """Export tag reads as CSV."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    reads = _application.get_reads()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Bib', 'Time', 'Reader', 'Timestamp', 'Lap', 'Position'])

    for read in reads:
        writer.writerow([
            read['bib'],
            read['time'],
            read['reader_id'],
            read['timestamp'],
            read.get('lap_number', ''),
            read.get('position', ''),
        ])

    output.seek(0)

    filename = f"reads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


def _parse_time_to_ms(time_str: str) -> Optional[int]:
    """Parse time string (HH:MM:SS.sss or similar) to milliseconds since midnight."""
    import re
    if not time_str:
        return None

    time_str = time_str.strip()

    # Match various formats
    patterns = [
        (r'^(\d{1,2}):(\d{2}):(\d{2})\.(\d{1,3})$', 'hms_ms'),  # HH:MM:SS.sss
        (r'^(\d{1,2}):(\d{2}):(\d{2})$', 'hms'),                # HH:MM:SS
        (r'^(\d{1,2}):(\d{2})\.(\d{1,3})$', 'ms_ms'),           # MM:SS.sss
        (r'^(\d{1,2}):(\d{2})$', 'ms'),                         # MM:SS
    ]

    for pattern, fmt in patterns:
        match = re.match(pattern, time_str)
        if match:
            groups = match.groups()
            hours, minutes, seconds, ms = 0, 0, 0, 0

            if fmt == 'hms_ms':
                hours, minutes, seconds = int(groups[0]), int(groups[1]), int(groups[2])
                ms = int(groups[3].ljust(3, '0'))
            elif fmt == 'hms':
                hours, minutes, seconds = int(groups[0]), int(groups[1]), int(groups[2])
            elif fmt == 'ms_ms':
                minutes, seconds = int(groups[0]), int(groups[1])
                ms = int(groups[2].ljust(3, '0'))
            elif fmt == 'ms':
                minutes, seconds = int(groups[0]), int(groups[1])

            return (hours * 3600 + minutes * 60 + seconds) * 1000 + ms

    return None


def _format_elapsed_time(ms: int) -> str:
    """Format milliseconds to elapsed time string (M:SS.ss or H:MM:SS.ss)."""
    if ms < 0:
        ms = 0
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    centiseconds = (ms % 1000) // 10

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}.{centiseconds:02d}"


@app.route('/api/reads/export/lif', methods=['GET', 'POST'])
def api_export_reads_lif():
    """Export tag reads as FinishLynx .lif format.

    POST JSON body:
    - event_number: Event number (e.g., "1" or "001")
    - round_number: Round number (e.g., "1")
    - heat_number: Heat number (e.g., "1")
    - event_name: Event name (e.g., "Boys 800m")
    - distance: Distance (e.g., "800")
    - start: Race start time (HH:MM:SS.S) to calculate elapsed times
    - save_directory: Optional directory to save the .lif file

    .lif format (comma-separated):
    - Header line (13 fields): Event#,Round,Heat,Name,Wind,WindUnit,Template,CaptureTime,CaptureDuration,Distance,StartTime,WindMode,LapTimeInfo
    - Result lines (19 fields): Place,ID,Lane,Last,First,Affil,Time,License,Delta,ReacTime,Splits,TTStart,User1,User2,User3,Delta2,Delta3,Speed,Pace
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    reads = _application.get_reads()

    # Also get pending reads that haven't been sent yet
    pending_reads_data = []
    with _validation_lock:
        for bib, pdata in _validation_state['pending_bibs'].items():
            for read in pdata.get('reads', []):
                ts = read.get('timestamp')
                if ts:
                    # Format timestamp for display
                    if hasattr(ts, 'strftime'):
                        time_str = ts.strftime('%H:%M:%S.') + f'{ts.microsecond // 1000:03d}'
                    else:
                        time_str = str(ts)
                    pending_reads_data.append({
                        'bib': bib,
                        'time': time_str,
                        'timestamp': str(ts),
                        'position': 0,
                        'is_pending': True,
                    })

    # Parse request data (support both GET query params and POST JSON)
    if request.method == 'POST':
        data = request.get_json() or {}
        event_number = data.get('event_number', '1')
        round_number = data.get('round_number', '1')
        heat_number = data.get('heat_number', '1')
        event_name = data.get('event_name', '')
        distance = data.get('distance', '')
        start_time_str = data.get('start', '')
        save_directory = data.get('save_directory', '')
        custom_filename = data.get('filename', '')
        pending_bibs = data.get('pending_bibs', [])  # Bibs that were neither accepted nor rejected
    else:
        event_number = request.args.get('event_number', '1')
        round_number = request.args.get('round_number', '1')
        heat_number = request.args.get('heat_number', '1')
        event_name = request.args.get('event_name', '')
        distance = request.args.get('distance', '')
        start_time_str = request.args.get('start', '')
        save_directory = ''
        custom_filename = ''
        pending_bibs = []

    start_time_ms = _parse_time_to_ms(start_time_str) if start_time_str else None

    # Build competitor data from reads (group by bib, get finish time)
    # Include both regular reads and pending reads
    all_reads = reads + pending_reads_data
    bib_data = {}
    for read in all_reads:
        bib = read['bib']
        if bib not in bib_data:
            bib_data[bib] = {
                'bib': bib,
                'time': read['time'],
                'timestamp': read['timestamp'],
                'position': read.get('position', 0),
            }
        # For multi-lap, take the last read (finish time)
        else:
            # Update with most recent read
            bib_data[bib]['time'] = read['time']
            bib_data[bib]['timestamp'] = read['timestamp']
            if read.get('position'):
                bib_data[bib]['position'] = read['position']

    # Sort by position (finish order)
    sorted_bibs = sorted(bib_data.values(), key=lambda x: x.get('position', 999))

    # Build .lif content (comma-separated)
    lines = []

    # Header line (13 comma-separated fields)
    # Event#, Round, Heat, EventName, Wind, WindUnit, Template, CaptureTime,
    # CaptureDuration, Distance, StartTime, WindMode, LapTimeInfo
    header_fields = [
        event_number,           # Event number
        round_number,           # Round number
        heat_number,            # Heat number
        event_name,             # Event name
        '',                     # Wind
        '',                     # Wind unit
        '',                     # Template
        '',                     # Capture time
        '',                     # Capture duration
        distance,               # Distance
        start_time_str,         # Start time (TOD)
        '',                     # Wind mode
        '',                     # Laptime info
    ]
    lines.append(','.join(header_fields))

    # Result lines (19 comma-separated fields per line)
    # Place, ID, Lane, LastName, FirstName, Affiliation, Time, License,
    # DeltaTime, ReacTime, Splits, TTStartTime, User1, User2, User3,
    # DeltaTime2, DeltaTime3, Speed, Pace
    first_time_ms = None
    for idx, data in enumerate(sorted_bibs, 1):
        place = str(idx)
        bib = data['bib']
        time_str = data['time']
        delta_str = ''

        # Calculate elapsed time if start time provided
        if start_time_ms is not None:
            read_time_ms = _parse_time_to_ms(time_str)
            if read_time_ms is not None:
                elapsed_ms = read_time_ms - start_time_ms
                # Handle day rollover
                if elapsed_ms < 0:
                    elapsed_ms += 24 * 3600 * 1000
                time_str = _format_elapsed_time(elapsed_ms)

                # Calculate delta from first finisher
                if first_time_ms is None:
                    first_time_ms = elapsed_ms
                else:
                    delta_ms = elapsed_ms - first_time_ms
                    delta_str = _format_elapsed_time(delta_ms)

        # Look up athlete info from .ppl data
        athlete = get_athlete_info(bib)
        last_name = athlete.get('last_name', '') if athlete else ''
        first_name = athlete.get('first_name', '') if athlete else ''
        school = athlete.get('school', '') if athlete else ''

        # Get lane info if in relay mode
        lane = ''
        relay_info = get_relay_display_info(bib)
        if relay_info and relay_info.get('lane'):
            lane = str(relay_info['lane'])

        # Build result line (19 fields)
        result_fields = [
            place,              # Place
            bib,                # ID (bib number)
            lane,               # Lane
            last_name,          # Last name
            first_name,         # First name
            school,             # Affiliation
            time_str,           # Time
            '',                 # License
            delta_str,          # Delta time
            '',                 # ReacTime
            '',                 # Splits
            start_time_str,     # Time trial start time
            '',                 # User 1
            '',                 # User 2
            '',                 # User 3
            delta_str,          # Delta time 2
            delta_str,          # Delta time 3
            '',                 # Speed
            '',                 # Pace
        ]
        lines.append(','.join(result_fields))

    # Add pending bibs note if there are any unresolved pending bibs
    if pending_bibs:
        # Add blank lines for separation
        lines.append('')
        lines.append('')
        # Add comment lines for each pending bib
        for pending_bib in pending_bibs:
            lines.append(f"; Bib {pending_bib}'s reads were neither accepted nor rejected")

    content = '\r\n'.join(lines) + '\r\n'

    # Generate filename - use custom if provided, otherwise time-based default
    if custom_filename:
        # Ensure .lif extension
        filename = custom_filename if custom_filename.endswith('.lif') else f"{custom_filename}.lif"
        try:
            filename = _bare_name(filename, '.lif')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
    else:
        # Default to time-based filename
        filename = f"results_{datetime.now().strftime('%H%M%S')}.lif"

    # Save to directory if specified
    if save_directory and os.path.isdir(save_directory):
        filepath = os.path.join(save_directory, filename)
        try:
            with open(filepath, 'w', newline='') as f:
                f.write(content)
            return jsonify({
                'success': True,
                'filepath': filepath,
                'filename': filename,
                'message': f'Saved {filename} to {save_directory}'
            })
        except Exception as e:
            return jsonify({'error': f'Failed to save file: {str(e)}'}), 500

    # Return as download
    return Response(
        content,
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/api/reads/clear', methods=['POST'])
def api_clear_reads():
    """Clear all tag reads."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    _application.clear_reads()
    return jsonify({'success': True})


@app.route('/api/lif/save-adjusted', methods=['POST'])
def api_save_adjusted_lif():
    """Save an adjusted LIF file to the same directory as the original."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.get_json() or {}
    content = data.get('content', '')
    original_path = data.get('originalPath', '')
    try:
        filename = _bare_name(data.get('filename', 'adjusted.lif'), '.lif')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    if not content:
        return jsonify({'error': 'No content provided'}), 400

    # Try to save to the same directory as the original file
    save_dir = None

    # First try the original file's directory
    if original_path:
        original_dir = os.path.dirname(original_path)
        if original_dir and os.path.isdir(original_dir):
            save_dir = original_dir

    # Fall back to configured event directory
    if not save_dir and _application.config.event_directory:
        rfid_dir = os.path.join(_application.config.event_directory, 'RFID Events')
        if os.path.isdir(rfid_dir):
            save_dir = rfid_dir
        elif os.path.isdir(_application.config.event_directory):
            # Create RFID Events subdirectory
            try:
                os.makedirs(rfid_dir, exist_ok=True)
                save_dir = rfid_dir
            except OSError:
                save_dir = _application.config.event_directory

    # Fallback to current directory if nothing else works
    if not save_dir:
        save_dir = os.getcwd()

    # Save the file
    filepath = os.path.join(save_dir, filename)
    try:
        with open(filepath, 'w', newline='') as f:
            f.write(content)
        logger.info(f"Saved adjusted LIF file: {filepath}")
        return jsonify({
            'success': True,
            'filepath': filepath,
            'filename': filename,
            'message': f'Saved {filename}'
        })
    except Exception as e:
        logger.error(f"Failed to save adjusted LIF: {e}")
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500


@app.route('/api/finishlynx/resend', methods=['POST'])
def api_finishlynx_resend():
    """Re-send reads to FinishLynx only (no pane ingest, no read-log entry):
    recovery for reads that were filtered or sent while the link was down.
    Body: {"reads": [{"bib": "16", "clock": "08:19:05.944"}, ...]}"""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(force=True) or {}
    sent, failed = [], []
    fl = _application.finishlynx
    event_num, round_num, heat_num = _application._get_current_event_info()
    # Clocks come from the read log (raw mat times): correct them like a
    # live finish in the event FinishLynx has armed would be (per race).
    mgr = _race_manager()
    race_key = mgr.find_by_event(int(event_num), int(heat_num or 1)) if (mgr and event_num) else None
    corr = _application.film_correction(race_key)
    for item in d.get('reads', []):
        bib = str(item.get('bib', '')).strip()
        try:
            ts = _parse_clock(str(item.get('clock', '')))
        except ValueError:
            failed.append({'bib': bib, 'reason': 'bad clock'})
            continue
        if not bib.isdigit():
            failed.append({'bib': bib, 'reason': 'bad bib'})
            continue
        ts = ts - timedelta(seconds=corr)
        if fl and fl.is_connected and fl.send_split(bib, ts, event_num, round_num, heat_num):
            sent.append(bib)
        else:
            failed.append({'bib': bib, 'reason': 'FinishLynx not connected'})
    logger.info(f"Resent {len(sent)} read(s) to FinishLynx, {len(failed)} failed"
                + (f" (film offset {corr:+.3f} s applied)" if corr else ""))
    return jsonify({'success': not failed, 'sent': sent, 'failed': failed,
                    'film_offset_applied': corr})


@app.route('/api/publish', methods=['POST'])
def api_publish():
    """The Stations panel's Publishing switch. Body {"on": bool,
    "flush": bool}: off = reads are still heard, classified and backed
    up, but nothing goes to FinishLynx and no merged .lif is written; on
    with flush=true also sends the finish reads kept back while off."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(force=True) or {}
    if 'on' not in d:
        return jsonify({'error': '"on" required'}), 400
    out = _application.set_publishing(bool(d['on']), flush=bool(d.get('flush', False)))
    return jsonify(dict(out, ok=True))


@app.route('/api/film_offset', methods=['GET', 'POST'])
def api_film_offset():
    """The meet's learned mat-vs-film finish offset (see film_offset.py).

    GET: current stats. POST body, all optional:
      {"manual": 0.65}   override the learned value (seconds; positive =
                         RFID later than the film); {"manual": null} clears
      {"apply": false}   stop correcting (still learns)
      {"reset": true}    forget the learned samples
    """
    mgr = _race_manager()
    if not mgr:
        return jsonify({"success": False, "error": "race manager not enabled"}), 400
    if request.method == 'GET':
        return jsonify(mgr.film.stats())
    d = request.get_json(force=True) or {}
    manual = ...
    if 'manual' in d:
        if d['manual'] in (None, ''):
            manual = None
        else:
            try:
                manual = float(d['manual'])
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "manual must be seconds, e.g. 0.65"}), 400
            if abs(manual) > 30:
                return jsonify({"success": False, "error": "manual offset out of range (±30 s)"}), 400
    apply = bool(d['apply']) if 'apply' in d else None
    stats = mgr.set_film_offset(manual=manual, apply=apply, reset=bool(d.get('reset')))
    if stats["manual"] is not None:
        msg = f"Film offset {stats['manual']:+.3f} s (manual, every race)"
    else:
        learned = [r for r in stats["races"] if r["samples"]]
        msg = ("Film offset per race: " + ", ".join(f"race {r['race']} {r['mean']:+.3f} s ({r['samples']})"
                                                     for r in learned)
               if learned else "No film offset: nothing learned yet (each race starts at 0)")
    if not stats["apply"]:
        msg += " - NOT applied"
    stats["success"] = True
    stats["message"] = msg
    return jsonify(stats)


@app.route('/api/manual-read', methods=['POST'])
def api_manual_read():
    """Add a manual bib entry."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    bib = data.get('bib', '').strip()

    if not bib:
        return jsonify({'error': 'Bib number required'}), 400

    if not bib.isdigit():
        return jsonify({'error': 'Bib must be a number'}), 400

    timestamp = None
    if data.get('clock'):
        try:
            timestamp = _parse_clock(data['clock'])
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    success = _application.add_manual_read(bib, timestamp)
    if not success:
        return jsonify({
            'success': False,
            'error': 'Bib not in current heat entries',
            'bib': bib
        }), 400

    return jsonify({'success': True, 'bib': bib})


@app.route('/api/start', methods=['POST'])
def api_start():
    """Start reading."""
    logger.info("=== api_start called ===")
    if not _application:
        logger.error("api_start: Application not initialized")
        return jsonify({'error': 'Application not initialized'}), 503

    logger.info(f"api_start: Calling _application.start(), mode={_application.config.mode}")
    try:
        success = _application.start()
        logger.info(f"api_start: _application.start() returned success={success}")
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"api_start: Exception during start: {e}", exc_info=True)
        return jsonify({'error': str(e), 'success': False}), 500


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop reading."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    _application.stop()
    return jsonify({'success': True})


@app.route('/api/test/reader', methods=['POST'])
def api_test_reader():
    """Test RFID reader connection."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    result = _application.test_reader_connection()
    return jsonify(result)


@app.route('/api/test/finishlynx', methods=['POST'])
def api_test_finishlynx():
    """Test FinishLynx connection."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    result = _application.test_finishlynx_connection()
    return jsonify(result)


@app.route('/api/reset-dedup', methods=['POST'])
def api_reset_dedup():
    """Reset deduplication (for new race)."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    _application.reset_dedup()
    return jsonify({'success': True, 'message': 'Deduplication reset - all bibs can be read again'})


@app.route('/api/send-start', methods=['POST'])
def api_send_start():
    """Send start/zero time to FinishLynx."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    success = _application.send_start_time()
    return jsonify({'success': success})


@app.route('/api/test/time-sync', methods=['POST'])
def api_test_time_sync():
    """Test time synchronization between all systems."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    result = _application.test_time_sync()
    return jsonify(result)


@app.route('/api/reader/registers', methods=['GET'])
def api_reader_registers():
    """Diagnostic: dump Vanch holding registers via the live connection.
    ?addr=8000&count=64 (max 125). The tag FIFO block is refused."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    reader = _application.reader
    if not hasattr(reader, 'read_registers'):
        return jsonify({'success': False, 'error': 'not a Vanch reader'}), 400
    try:
        addr = int(request.args.get('addr', 8000))
        count = int(request.args.get('count', 64))
        words = reader.read_registers(addr, count)
    except (ValueError, OSError, IOError) as e:
        return jsonify({'success': False, 'error': str(e), 'addr': request.args.get('addr')}), 400
    return jsonify({'success': True, 'addr': addr, 'count': len(words),
                    'registers': [{'addr': addr + i, 'dec': w, 'hex': f'{w:04x}'} for i, w in enumerate(words)]})


@app.route('/api/reader/restart', methods=['POST'])
def api_reader_restart():
    """Drop and re-open the RFID reader with the current config."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    ok = _application.restart_reader()
    return jsonify({'success': ok,
                    'message': 'Reader reconnected' if ok else 'Reader did not answer (console stopped, or reader unreachable)'})


@app.route('/api/finishlynx/reconnect', methods=['POST'])
def api_finishlynx_reconnect():
    """Drop and re-dial the LapTime TCP link."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(_application.reconnect_finishlynx())


@app.route('/api/scoreboard/restart', methods=['POST'])
def api_scoreboard_restart():
    """Re-bind the scoreboard UDP listener."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    ok = _application.restart_scoreboard_listener()
    return jsonify({'success': ok,
                    'message': 'Scoreboard listener restarted' if ok else 'Console is stopped'})


@app.route('/api/lora/restart', methods=['POST'])
def api_lora_restart():
    """Re-open the LoRa gateway with the current config."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(_application.restart_lora())


@app.route('/api/camera-start/restart', methods=['POST'])
def api_camera_start_restart():
    """Restart the camera start-signal sniffer (after changing its settings).
    Body may carry {"enabled", "camera_host", "interface"} to update config."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(silent=True) or {}
    cs = _application.config.camera_start
    changed = False
    if 'enabled' in d:
        cs.enabled = bool(d['enabled']); changed = True
    if 'camera_host' in d:
        # blank (or auto) = find the camera from its replies (2026-10-11)
        from .camera_start import normalize_camera_host
        cs.camera_host = normalize_camera_host(d['camera_host']); changed = True
    if 'interface' in d:
        cs.interface = str(d['interface']).strip() or 'auto'; changed = True
    if changed:
        _application.save_config()
    return jsonify(_application.restart_camera_start())


@app.route('/api/clocks/gun', methods=['POST'])
def api_clocks_gun():
    """Operator control of what every LED clock shows.

    Body: {"mode": "elapsed", "elapsed_seconds": 754.0}  count up from 12:34
          {"mode": "now"}   start at 0:00 right now
          {"mode": "tod"}   force time of day
          {"mode": "race", "race": 3}  pin the clocks to pane 3's gun
          {"mode": "auto"}  back to following the race pane / FinishLynx clock
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(force=True) or {}
    try:
        secs = float(d['elapsed_seconds']) if d.get('elapsed_seconds') is not None else None
        race_key = int(d['race']) if d.get('race') not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'elapsed_seconds must be a number, race a pane key'}), 400
    result = _application.set_clock_override(str(d.get('mode', 'auto')), secs, race_key)
    return jsonify(result), (200 if result.get('success') else 400)


@app.route('/api/clocks/<port>', methods=['POST'])
def api_clock_update(port):
    """Rename, enable/disable or re-type one clock.
    Body: {"label": "...", "enabled": true, "type": "auto|ch340|electro|none"}."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(force=True) or {}
    label = d.get('label') if 'label' in d else None
    enabled = bool(d['enabled']) if 'enabled' in d else None
    kind = str(d['type']) if d.get('type') else None
    result = _application.update_clock(port, label=label, enabled=enabled, kind=kind)
    return jsonify(result), (200 if result.get('success') else 400)


@app.route('/api/clocks/<port>/retry', methods=['POST'])
def api_clock_retry(port):
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(_application.retry_clock(port))


@app.route('/api/discover/readers', methods=['POST'])
def api_discover_readers():
    """Scan network for Impinj RFID readers.

    If no reachable readers are found but mDNS discovers unreachable readers,
    returns them with needs_configuration=True flag.

    Query params:
        auto_configure=true - Automatically configure unreachable readers
    """
    from .app import discover_impinj_readers, auto_configure_reader

    try:
        configured_host = _application.config.reader.host if _application else None
        auto_config = request.args.get('auto_configure', 'false').lower() == 'true'

        results = discover_impinj_readers(configured_host)

        # Check if we have any verified (reachable) readers
        verified_readers = [r for r in results if r.get('verified', False)]
        unverified_readers = [r for r in results if not r.get('verified', False)]

        # If no verified readers but we have unverified ones, offer auto-configure
        if not verified_readers and unverified_readers:
            if auto_config:
                # Auto-configure the first unreachable reader
                logger.info("No reachable readers found, auto-configuring...")
                configured = auto_configure_reader()
                if configured:
                    # Update the application config
                    if _application:
                        _application.config.reader.host = configured['host']
                        _application.save_config()
                    return jsonify({
                        'success': True,
                        'devices': [configured],
                        'count': 1,
                        'message': f"Auto-configured reader at {configured['host']}",
                        'auto_configured': True,
                    })
                else:
                    return jsonify({
                        'success': False,
                        'devices': unverified_readers,
                        'count': len(unverified_readers),
                        'message': 'Found readers via mDNS but auto-configuration failed',
                        'needs_configuration': True,
                    })
            else:
                # Return unverified readers with flag
                for r in unverified_readers:
                    r['needs_configuration'] = True
                return jsonify({
                    'success': True,
                    'devices': unverified_readers,
                    'count': len(unverified_readers),
                    'message': f'Found {len(unverified_readers)} reader(s) via mDNS (not reachable - different subnet)',
                    'needs_configuration': True,
                })

        return jsonify({
            'success': True,
            'devices': results,
            'count': len(results),
            'message': f'Found {len(results)} reader(s)' if results else 'No readers found',
        })
    except Exception as e:
        logger.error(f"Discovery failed: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'devices': [],
            'count': 0,
            'message': f'Scan failed: {str(e)}',
        })


@app.route('/api/discover/finishlynx', methods=['POST'])
def api_discover_finishlynx():
    """Scan network for FinishLynx systems."""
    from .app import discover_finishlynx

    try:
        configured_host = _application.config.finishlynx.host if _application else None
        configured_port = _application.config.finishlynx.port if _application else 5000
        results = discover_finishlynx(configured_host, configured_port)
        return jsonify({
            'success': True,
            'devices': results,
            'count': len(results),
            'message': f'Found {len(results)} FinishLynx system(s)' if results else 'No FinishLynx found',
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'devices': [],
            'count': 0,
            'message': f'Scan failed: {str(e)}',
        })


@app.route('/api/discover/serial', methods=['POST'])
def api_discover_serial():
    """Scan for available serial ports (for LoRa devices)."""
    from .app import discover_serial_ports

    try:
        results = discover_serial_ports()
        return jsonify({
            'success': True,
            'ports': results,
            'count': len(results),
            'message': f'Found {len(results)} serial port(s)' if results else 'No serial ports found',
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'ports': [],
            'count': 0,
            'message': f'Scan failed: {str(e)}',
        })


@app.route('/api/reader/auto-configure', methods=['POST'])
def api_auto_configure_reader():
    """Automatically configure an Impinj reader to be on the local subnet.

    Body (optional): {
        "hostname": "SpeedwayR-14-C8-1D"  // specific reader to configure
    }

    This will:
    1. Discover readers via mDNS (.local addresses)
    2. Find a free IP on the local subnet
    3. Configure the reader with that IP via SSH
    4. Wait for the reader to reboot
    """
    from .app import auto_configure_reader

    data = request.json or {}
    hostname = data.get('hostname')

    try:
        result = auto_configure_reader(hostname)
        if result:
            # Update the config with the new IP
            if _application:
                _application.config.reader.host = result['host']
                _application.save_config()

            return jsonify({
                'success': True,
                'reader': result,
                'message': f"Reader configured at {result['host']}",
            })
        else:
            return jsonify({
                'success': False,
                'reader': None,
                'message': 'No reader found to configure, or configuration failed',
            })
    except Exception as e:
        logger.error(f"Auto-configure failed: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'reader': None,
            'message': f'Configuration failed: {str(e)}',
        })


@app.route('/api/remote/units')
def api_remote_units():
    """Get all remote units with status."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(_application.get_remote_units())


@app.route('/api/remote/units/register', methods=['POST'])
def api_remote_register():
    """Pre-register a remote unit by reader_id."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    reader_id = data.get('reader_id', '').strip()
    if not reader_id:
        return jsonify({'error': 'reader_id required'}), 400

    _application.add_pre_registered_unit(reader_id)
    return jsonify({'success': True, 'reader_id': reader_id})


@app.route('/api/remote/units/<reader_id>', methods=['DELETE'])
def api_remote_remove(reader_id):
    """Remove a remote unit."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    _application.remove_remote_unit(reader_id)
    return jsonify({'success': True})


@app.route('/api/remote/units/<reader_id>/command', methods=['POST'])
def api_remote_command(reader_id):
    """Send command to remote unit. Body: {"command": "START"|"STOP"|...}."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    command = data.get('command', '').upper()
    valid_commands = ('START', 'STOP', 'RESET_DEDUP', 'RESTART', 'GET_STATUS')
    if command not in valid_commands:
        return jsonify({'error': f'Invalid command. Valid: {valid_commands}'}), 400

    result = _application.send_remote_command(reader_id, command)
    return jsonify(result)


@app.route('/api/remote/units/<reader_id>/clock-check', methods=['POST'])
def api_remote_clock_check_start(reader_id):
    """Ping a station to measure the radio round trip and its clock error.
    Body: {"count": 10}. Runs in the background; poll with GET."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    count = int((request.json or {}).get('count', 10))
    if not 1 <= count <= 50:
        return jsonify({'error': 'count must be 1-50'}), 400
    result = _application.start_clock_check(reader_id, count)
    return jsonify(result), (200 if result.get('success') else 409)


@app.route('/api/remote/units/<reader_id>/clock-check')
def api_remote_clock_check(reader_id):
    """Progress / result of the latest clock check (seconds; offset + = the
    station's read times are late)."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(_application.get_clock_check(reader_id))


@app.route('/api/remote/units/<reader_id>/config', methods=['GET'])
def api_remote_get_config(reader_id):
    """Get remote unit config. Query param: ?section=all|reader|timing|lora|logging."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    section = request.args.get('section', 'all')
    result = _application.get_remote_config(reader_id, section)
    return jsonify(result)


@app.route('/api/remote/units/<reader_id>/config', methods=['POST'])
def api_remote_set_config(reader_id):
    """Set remote unit config. Body: {"section": "timing", "values": {...}}."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    section = data.get('section', '')
    values = data.get('values', {})
    if not section or not values:
        return jsonify({'error': 'section and values required'}), 400

    result = _application.set_remote_config(reader_id, section, values)
    return jsonify(result)


@app.route('/api/logs')
def api_get_logs():
    """Get recent log entries. Optional ?level=WARNING,ERROR filter."""
    level_filter = request.args.get('level', '').split(',') if request.args.get('level') else None
    logs = _log_handler.get_logs(level_filter)
    return jsonify(logs)


@app.route('/api/logs/export')
def api_export_logs():
    """Export log entries as a .txt file."""
    logs = _log_handler.get_logs()
    lines = []
    for entry in logs:
        lines.append(f"[{entry['timestamp']}] {entry['level']:8s} {entry['message']}")
    content = '\n'.join(lines)
    return Response(
        content,
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename=rfid_log_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'}
    )


@app.route('/api/logs/clear', methods=['POST'])
def api_clear_logs():
    """Clear the log buffer."""
    _log_handler.clear()
    return jsonify({'success': True})


def _exit_soon(delay: float = 0.5):
    """Shut the app down after the response is sent; start-console.bat's
    loop starts it again."""
    if _application:
        _application.shutdown()

    def do_exit():
        import time
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=do_exit, daemon=True).start()


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """Gracefully shut down the middleware."""
    logger.info("Shutdown requested via web UI")
    _exit_soon()
    return jsonify({'success': True, 'message': 'Shutting down...'})


# --- Updates from GitHub and "Send logs" (updater.py, support.py) ---

def _busy_reasons() -> list:
    """Why restarting now would hurt: a racing pane, FinishLynx's clock."""
    out = []
    mgr = _race_manager()
    if mgr:
        racing = [r.name for r in mgr.races.values() if r.status == "racing"]
        if racing:
            out.append("racing: " + ", ".join(racing))
    try:
        if _application and _application._finishlynx_clock_gun() is not None:
            out.append("FinishLynx's race clock is running")
    except Exception:
        pass
    return out


@app.route('/api/update/check')
def api_update_check():
    from . import updater
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    return jsonify(updater.check(_application.config.support.update_repo))


@app.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Download the release, install it, restart. Body {"force": true} goes
    ahead while a race runs."""
    from . import updater
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(silent=True) or {}
    busy = _busy_reasons()
    if busy and not d.get('force'):
        return jsonify({'success': False, 'busy': busy})
    try:
        zip_bytes = updater.download(_application.config.support.update_repo)
        out = updater.apply(zip_bytes)
    except updater.UpdateError as e:
        return jsonify({'success': False, 'error': str(e)})
    except Exception as e:
        logger.exception("Update failed")
        return jsonify({'success': False, 'error': f'update failed: {e}'})
    logger.info("Restarting on the new version")
    _exit_soon(1.5)
    return jsonify(dict(out, success=True))


@app.route('/api/update/rollback', methods=['POST'])
def api_update_rollback():
    from . import updater
    try:
        out = updater.rollback()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    _exit_soon(1.5)
    return jsonify(dict(out, success=True))


@app.route('/api/support/send', methods=['POST'])
def api_support_send():
    from . import support
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    note = str((request.get_json(silent=True) or {}).get('note', ''))[:2000]
    try:
        return jsonify(dict(support.send(_application, note), success=True))
    except support.SendError as e:
        return jsonify({'success': False, 'error': str(e)})
    except Exception as e:
        logger.exception("Send logs failed")
        return jsonify({'success': False, 'error': f'sending failed: {e}'})


@app.route('/api/support/settings', methods=['POST'])
def api_support_settings():
    """{"token": "..."} sets the support key ("" clears it); {"site_name"}.
    The key is stored in config.yaml and never sent back to a page."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503
    d = request.get_json(silent=True) or {}
    sp = _application.config.support
    if 'token' in d:
        sp.logs_token = str(d['token'] or '').strip()
    if 'site_name' in d:
        sp.site_name = str(d['site_name'] or '').strip()[:60]
    _application.save_config()
    return jsonify({'success': True, 'token_set': _support_key_set(_application.config),
                    'site_name': sp.site_name})


# --- Athlete/PPL API endpoints ---

@app.route('/api/fs/dirs')
def api_fs_dirs():
    """Folder picker for the Lynx folder dialog (2026-10-03, user's ask: a
    browser's own picker never gives the console a full path). Subfolder
    names of `path` only - no files, no contents - plus its parent, the
    drives on Windows, and whether lynx.evt/.ppl or an RFID folder is there.
    `child` (a name from `dirs`) is joined here, so the page never builds paths.
    No path: the current event directory, else home; a path that is gone
    walks up to the nearest folder that exists."""
    path = request.args.get('path', '').strip()
    if not path:
        path = (_application.config.event_directory if _application else '') or os.path.expanduser('~')
    child = request.args.get('child', '').strip()
    if child:
        path = os.path.join(path, child)
    path = os.path.abspath(os.path.expanduser(path))
    while not os.path.isdir(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    dirs, files = [], set()
    try:
        for e in os.scandir(path):
            try:
                if e.is_dir():
                    if not e.name.startswith(('.', '$')):
                        dirs.append(e.name)
                else:
                    files.add(e.name.lower())
            except OSError:
                continue
    except OSError as e:
        return jsonify({'error': str(e), 'path': path}), 400
    dirs.sort(key=str.lower)
    parent = os.path.dirname(path)
    drives = []
    if os.name == 'nt':
        drives = [f'{c}:\\' for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if os.path.isdir(f'{c}:\\')]
    return jsonify({'path': path, 'parent': None if parent == path else parent, 'dirs': dirs,
                    'drives': drives, 'lynx_files': sorted(files & {'lynx.evt', 'lynx.ppl', 'lynx.sch'}),
                    'has_rfid': any(d.lower() == 'rfid' for d in dirs)})


# Native file dialogs (2026-10-03, user's ask), run in their own process so
# Tk never shares the server's threads. Windows timing PCs: tkinter (the
# modern Explorer dialogs; Tcl/Tk ships with the installed Python). Linux
# dev: zenity. They open on the console PC's own screen, so only a browser on
# that PC may ask for one; anything else gets 403 and the page falls back.
_TK_PICK = (
    "import sys, json, tkinter as tk; from tkinter import filedialog as fd\n"
    "a = json.loads(sys.argv[1]); r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "ft = ([(a['desc'] or a['ext'], '*' + a['ext'])] if a['ext'] else []) + [('All files', '*.*')]\n"
    "if a['mode'] == 'dir': p = fd.askdirectory(parent=r, initialdir=a['start'], title=a['title'], mustexist=True)\n"
    "elif a['mode'] == 'open': p = fd.askopenfilename(parent=r, initialdir=a['start'], title=a['title'], filetypes=ft)\n"
    "else: p = fd.asksaveasfilename(parent=r, initialdir=a['start'], initialfile=a['name'], title=a['title'], "
    "defaultextension=a['ext'], filetypes=ft, confirmoverwrite=True)\n"
    "print(p or '')")
_LAST_DIR = {}          # mode -> folder last picked in, for the next dialog


def _local_request() -> bool:
    host = request.host.rsplit(':', 1)[0].strip('[]')
    return request.remote_addr in ('127.0.0.1', '::1', host)


def _native_pick(mode: str, start: str = '', title: str = '', name: str = '', ext: str = '', desc: str = ''):
    """mode dir | open | save -> (path or None if cancelled, error or None)."""
    import json as _json
    import shutil
    import subprocess
    import sys
    start = os.path.abspath(start) if start else ''
    if not start or not os.path.isdir(start):
        start = _LAST_DIR.get(mode) or (_application.config.event_directory if _application else '') or ''
        if not os.path.isdir(start):
            start = os.path.expanduser('~')
    title = title or {'dir': 'Choose a folder', 'open': 'Open', 'save': 'Save as'}[mode]
    if os.name != 'nt' and shutil.which('zenity'):
        cmd = ['zenity', '--file-selection', '--title=' + title]
        if mode == 'dir':
            cmd += ['--directory', '--filename=' + os.path.join(start, '')]
        else:
            if mode == 'save':
                cmd += ['--save', '--confirm-overwrite', '--filename=' + os.path.join(start, name)]
            else:
                cmd += ['--filename=' + os.path.join(start, '')]
            if ext:
                cmd += [f'--file-filter={desc or ext} | *{ext}', '--file-filter=All files | *']
    else:
        cmd = [sys.executable, '-c', _TK_PICK, _json.dumps({'mode': mode, 'start': start, 'title': title,
                                                             'name': name, 'ext': ext, 'desc': desc})]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=600,
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                           creationflags=0x08000000 if os.name == 'nt' else 0)   # CREATE_NO_WINDOW
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f'no file dialog: {e}'
    path = r.stdout.strip()
    if not path:
        if r.returncode not in (0, 1):                     # zenity: 1 = cancelled
            return None, 'no file dialog: ' + (r.stderr.strip().splitlines() or ['failed'])[-1]
        return None, None
    path = os.path.normpath(path)
    _LAST_DIR[mode] = path if mode == 'dir' else os.path.dirname(path)
    return path, None


@app.route('/api/fs/pick', methods=['POST'])
def api_fs_pick():
    """The OS dialog: {mode: dir|open|save, path?, title?, name?, ext?, desc?}
    -> {"path": picked} ({"path": null} = cancelled). Lynx folder Browse…,
    Open another meet's Browse…"""
    if not _local_request():
        return jsonify({'error': 'the file dialog opens on the console PC'}), 403
    d = request.get_json(silent=True) or {}
    mode = d.get('mode', 'dir')
    if mode not in ('dir', 'open', 'save'):
        return jsonify({'error': 'mode must be dir, open or save'}), 400
    path, err = _native_pick(mode, str(d.get('path') or '').strip(), str(d.get('title') or ''),
                             str(d.get('name') or ''), str(d.get('ext') or ''), str(d.get('desc') or ''))
    if err:
        return jsonify({'error': err}), 501
    return jsonify({'path': path})


@app.route('/api/fs/save', methods=['POST'])
def api_fs_save():
    """Save as… through the OS dialog (2026-10-03): {url, name, ext, desc} -
    the console fetches its own `url` (an /api/ export or printout), asks
    where it goes (name + folder, last folder remembered) and writes it
    there. {"path": null} = cancelled. 403 off the console PC / 501 with no
    dialog: the page then uses the browser's own save or a download."""
    if not _local_request():
        return jsonify({'error': 'the file dialog opens on the console PC'}), 403
    d = request.get_json(silent=True) or {}
    url = str(d.get('url') or '')
    if not url.startswith('/api/') or url.startswith('/api/fs/'):
        return jsonify({'error': 'not an export'}), 400
    with app.test_client() as c:
        r = c.get(url)
    if r.status_code != 200:
        try:
            err = (r.get_json(silent=True) or {}).get('error')
        except Exception:
            err = None
        return jsonify({'error': err or f'export failed ({r.status_code})'}), 400
    name = os.path.basename(str(d.get('name') or 'export'))
    path, err = _native_pick('save', '', 'Save as', name, str(d.get('ext') or ''), str(d.get('desc') or ''))
    if err:
        return jsonify({'error': err}), 501
    if not path:
        return jsonify({'path': None})
    try:
        with open(path, 'wb') as f:
            f.write(r.get_data())
    except OSError as e:
        return jsonify({'error': f'could not write {path}: {e}'}), 500
    logger.info(f"Saved {url.split('?')[0]} to {path}")
    return jsonify({'path': path})


@app.route('/api/athletes/load', methods=['POST'])
def api_load_athletes():
    """Load athlete data from lynx.ppl in the specified directory."""
    data = request.json
    directory = data.get('directory', '')

    if not directory:
        return jsonify({'error': 'directory required'}), 400

    success = load_ppl_from_directory(directory)
    if success:
        # Remember the meet folder across restarts: athletes reload at boot
        # and .lif streaming defaults into this directory.
        if _application and _application.config.event_directory != directory:
            _application.config.event_directory = directory
            try:
                _application.save_config()
            except Exception:
                logger.exception("Could not persist event_directory")
        # The meet folder normally carries lynx.evt too: pick it up so the
        # race picker and bib filtering work without a second step. Panes
        # are NOT created here - the operator picks which scheduled races
        # to time from the "Races from .evt" picker (a track .evt can hold
        # dozens of events that never see the RFID mat).
        evt_races = 0
        evt_missing = 0
        try:
            content = _autoload_evt(directory)
            mgr = _race_manager()
            if content:
                races = evt_race_list(content)
                evt_races = len(races)
                existing = set()
                if mgr:
                    with mgr._lock:
                        existing = {(r.event, r.round, r.heat) for r in mgr.races.values()}
                evt_missing = sum(1 for r in races
                                  if (r['event'], r['round'], r['heat']) not in existing)
        except Exception:
            logger.exception("Could not load .evt from meet folder")
        with _athlete_lock:
            count = len(_athlete_data['by_bib'])
            schools = len(_athlete_data['by_school_gender'])
        return jsonify({
            'success': True,
            'athletes_loaded': count,
            'schools_loaded': schools,
            'evt_races': evt_races,
            'evt_missing': evt_missing,
            'message': f'Loaded {count} athletes from {schools} schools'
                       + (f', {evt_races} race(s) in the .evt' if evt_races else '')
        })
    else:
        return jsonify({
            'success': False,
            'message': 'No lynx.ppl file found or failed to parse'
        })


@app.route('/api/athletes/<bib>')
def api_get_athlete(bib):
    """Get athlete info by bib number."""
    info = get_athlete_info(bib)
    if info:
        return jsonify({'success': True, 'athlete': info})
    return jsonify({'success': False, 'error': 'Athlete not found'})


@app.route('/api/athletes')
def api_get_all_athletes():
    """Get all loaded athlete data."""
    with _athlete_lock:
        return jsonify({
            'success': _athlete_data['loaded'],
            'by_bib': _athlete_data['by_bib'],
            'schools': list(_athlete_data['by_school_gender'].keys()),
        })


# --- Relay API endpoints ---

@app.route('/api/relay/parse-evt', methods=['POST'])
def api_parse_relay_evt():
    """Parse relay heat data and all event entries from .evt file content."""
    global _relay_data, _event_data

    data = request.json
    content = data.get('content', '')

    if not content:
        return jsonify({'error': 'content required'}), 400

    # Parse relay-specific heat data
    heats = parse_relay_heats_from_evt(content)
    with _relay_lock:
        _relay_data['heats'] = heats

    # Also parse all event entries for validation
    all_entries = parse_all_heat_entries(content)
    with _event_data_lock:
        _event_data['heats'] = all_entries

    # Build a list of relay events for the dropdown
    relay_events = []
    for (event_num, heat_num), lane_data in heats.items():
        teams = [f"{d['team_name']} (Lane {lane})" for lane, d in lane_data.items()]
        relay_events.append({
            'event_num': event_num,
            'heat_num': heat_num,
            'teams': teams,
            'lane_count': len(lane_data),
        })

    return jsonify({
        'success': True,
        'heats_found': len(heats),
        'relay_events': relay_events,
        'total_entries_parsed': len(all_entries),
    })


@app.route('/api/relay/set-event', methods=['POST'])
def api_set_relay_event():
    """Set the current relay event/heat (also sets validation heat)."""
    data = request.json
    event_num = data.get('event_num', '')
    heat_num = data.get('heat_num', '')
    event_name = data.get('event_name', '')

    # Set relay event
    set_current_relay_event(event_num, heat_num, event_name)

    # Also set validation heat
    set_current_heat(event_num, heat_num)

    heat_info = _event_data.get('current_heat_info')

    with _relay_lock:
        return jsonify({
            'success': True,
            'is_relay': _relay_data['is_relay'],
            'heat_data': _relay_data['current_heat_data'],
            'teams': list(_relay_state['team_legs'].keys()) if _relay_data['is_relay'] else [],
            'validation': {
                'gender': heat_info.get('gender', '') if heat_info else '',
                'bibs': list(heat_info.get('bibs', set())) if heat_info else [],
                'schools': list(heat_info.get('schools', set())) if heat_info else [],
            },
        })


@app.route('/api/relay/state')
def api_get_relay_state():
    """Get current relay state."""
    with _relay_lock:
        return jsonify({
            'is_relay': _relay_data['is_relay'],
            'current_event': _relay_data['current_event'],
            'heat_data': _relay_data['current_heat_data'],
            'team_legs': {k: {
                'current_leg': v['current_leg'],
                'bibs': v['bibs'],
                'lane': v['lane'],
                'school': v['school'],
            } for k, v in _relay_state['team_legs'].items()},
            'bib_assignments': _relay_state['bib_assignments'],
            'retired_bibs': list(_relay_state['retired_bibs']),
        })


@app.route('/api/relay/reset', methods=['POST'])
def api_reset_relay_state():
    """Reset relay state for a new race."""
    global _relay_state

    with _relay_lock:
        # Reset state but keep heat data
        _relay_state['team_legs'] = {}
        _relay_state['bib_assignments'] = {}
        _relay_state['retired_bibs'] = set()
        _relay_state['school_team_order'] = {}

        # Re-initialize from current heat data if set
        if _relay_data['current_heat_data']:
            for lane, data in _relay_data['current_heat_data'].items():
                team_name = data['team_name']
                school = data['school']

                _relay_state['team_legs'][team_name] = {
                    'current_leg': 1,
                    'bibs': {},
                    'lane': lane,
                    'school': school,
                    'read_count': {},
                }

                if school not in _relay_state['school_team_order']:
                    _relay_state['school_team_order'][school] = []
                _relay_state['school_team_order'][school].append(team_name)

            for school in _relay_state['school_team_order']:
                _relay_state['school_team_order'][school].sort()

    return jsonify({'success': True, 'message': 'Relay state reset'})


# --- Bib Validation API endpoints ---

@app.route('/api/validation/parse-evt', methods=['POST'])
def api_parse_evt_entries():
    """Parse all event entries from .evt file content for validation."""
    global _event_data

    data = request.json
    content = data.get('content', '')

    if not content:
        return jsonify({'error': 'content required'}), 400

    heats = parse_all_heat_entries(content)
    with _event_data_lock:
        _event_data['heats'] = heats

    # Summary stats
    total_bibs = sum(len(h['bibs']) for h in heats.values())
    total_schools = sum(len(h['schools']) for h in heats.values())
    relay_heats = sum(1 for h in heats.values() if h['is_relay'])

    return jsonify({
        'success': True,
        'heats_found': len(heats),
        'total_individual_entries': total_bibs,
        'total_relay_schools': total_schools,
        'relay_heats': relay_heats,
    })


@app.route('/api/validation/set-heat', methods=['POST'])
def api_set_validation_heat():
    """Set the current heat for validation."""
    data = request.json
    event_num = data.get('event_num', '')
    heat_num = data.get('heat_num', '')

    set_current_heat(event_num, heat_num)

    heat_info = _event_data.get('current_heat_info')
    return jsonify({
        'success': True,
        'heat_info': {
            'bibs': list(heat_info.get('bibs', set())) if heat_info else [],
            'schools': list(heat_info.get('schools', set())) if heat_info else [],
            'gender': heat_info.get('gender', '') if heat_info else '',
            'is_relay': heat_info.get('is_relay', False) if heat_info else False,
            'event_name': heat_info.get('event_name', '') if heat_info else '',
        } if heat_info else None,
    })


@app.route('/api/validation/state')
def api_get_validation_state():
    """Get current validation state."""
    with _validation_lock:
        return jsonify({
            'pending_bibs': {
                bib: {
                    'reason': data['reason'],
                    'read_count': len(data['reads']),
                }
                for bib, data in _validation_state['pending_bibs'].items()
            },
            'blacklisted_bibs': list(_validation_state['blacklisted_bibs']),
            'accepted_bibs': list(_validation_state['accepted_bibs']),
            'current_heat': _event_data.get('current_event'),
        })


@app.route('/api/validation/accept', methods=['POST'])
def api_accept_bib():
    """Accept a pending bib - release held reads to FinishLynx."""
    data = request.json
    bib = data.get('bib', '')

    if not bib:
        return jsonify({'error': 'bib required'}), 400

    # Accept the bib and get held reads
    held_reads = accept_pending_bib(bib)

    # Process and send each held read to FinishLynx
    if _application and held_reads:
        for read_data in held_reads:
            # Re-process the read now that bib is accepted
            timestamp = read_data.get('timestamp')
            if timestamp:
                # The bib is now accepted, so it will pass validation
                # We need to manually trigger sending to FinishLynx
                _application.process_accepted_bib(bib, timestamp)

    # Emit acceptance to UI
    socketio.emit('bib_accepted', {'bib': bib, 'reads_released': len(held_reads)})

    return jsonify({
        'success': True,
        'bib': bib,
        'reads_released': len(held_reads),
    })


@app.route('/api/validation/reject', methods=['POST'])
def api_reject_bib():
    """Reject a pending bib - add to blacklist."""
    data = request.json
    bib = data.get('bib', '')

    if not bib:
        return jsonify({'error': 'bib required'}), 400

    reject_pending_bib(bib)

    # Emit rejection to UI
    socketio.emit('bib_rejected', {'bib': bib})

    return jsonify({
        'success': True,
        'bib': bib,
    })


@app.route('/api/validation/reset', methods=['POST'])
def api_reset_validation():
    """Reset validation state (pending, blacklisted, accepted bibs)."""
    reset_validation_state()

    return jsonify({
        'success': True,
        'message': 'Validation state reset',
    })


# --- Operation Mode and TDP Status API ---

@app.route('/api/operation-mode', methods=['GET'])
def api_get_operation_mode():
    """Get current operation mode."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    return jsonify({
        'operation_mode': _application.operation_mode,
        'manual_race_in_progress': _application.manual_race_in_progress,
    })


@app.route('/api/operation-mode', methods=['POST'])
def api_set_operation_mode():
    """Set operation mode.

    Request body: {"mode": "current_reads" | "finishlynx"}
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.get_json()
    if not data or 'mode' not in data:
        return jsonify({'error': 'Missing mode parameter'}), 400

    mode = data['mode']
    if mode not in ('current_reads', 'finishlynx'):
        return jsonify({'error': 'Invalid mode. Must be "current_reads" or "finishlynx"'}), 400

    success = _application.set_operation_mode(mode)
    if not success:
        return jsonify({
            'error': 'Cannot change mode: manual race in progress',
            'manual_race_in_progress': _application.manual_race_in_progress,
        }), 409

    return jsonify({
        'success': True,
        'operation_mode': _application.operation_mode,
    })


@app.route('/api/tdp-status', methods=['GET'])
def api_tdp_status():
    """Get TDP (FinishLynx LDP connection) status.

    Returns TDP connection state, running time, and current event/heat.
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    status = {
        'tdp_connected': False,
        'running_time': 0.0,
        'event': 0,
        'heat': 0,
    }

    if _application.scoreboard_listener:
        status['tdp_connected'] = _application.scoreboard_listener.tdp_connected
        status['event'] = _application.scoreboard_listener.event
        status['heat'] = _application.scoreboard_listener.heat

    return jsonify(status)


@app.route('/api/manual-race/save', methods=['POST'])
def api_save_manual_race():
    """Save pending race results after TDP disconnect.

    Request body: {"event": int, "heat": int}
    """
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    if not _application.manual_race_in_progress:
        return jsonify({'error': 'No manual race in progress'}), 400

    data = request.get_json()
    if not data or 'event' not in data or 'heat' not in data:
        return jsonify({'error': 'Missing event or heat parameter'}), 400

    try:
        event_num = int(data['event'])
        heat_num = int(data['heat'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid event or heat number'}), 400

    success = _application.save_manual_race(event_num, heat_num)
    if not success:
        return jsonify({'error': 'Failed to save manual race'}), 500

    return jsonify({
        'success': True,
        'message': f'Race saved: Event {event_num}, Heat {heat_num}',
    })


@app.route('/api/manual-race/clear', methods=['POST'])
def api_clear_manual_race():
    """Discard pending race results after TDP disconnect."""
    if not _application:
        return jsonify({'error': 'Application not initialized'}), 503

    if not _application.manual_race_in_progress:
        return jsonify({'error': 'No manual race in progress'}), 400

    success = _application.clear_manual_race()
    if not success:
        return jsonify({'error': 'Failed to clear manual race'}), 500

    return jsonify({
        'success': True,
        'message': 'Race results discarded',
    })


# Template mapping: event name patterns -> template base names
# Actual template will have _FL (finish line only) or _SPLITS (200m split) suffix
# IMPORTANT: Relay patterns must come BEFORE individual event patterns to match correctly
# (e.g., "4 x 800" must be checked before "800" to avoid false matches)
TRACK_EVENT_TEMPLATES = [
    ('4 x 800', 'TEMPLATE_4x800m'),   # Matches "4 x 800m Relay"
    ('4x800', 'TEMPLATE_4x800m'),     # Matches "4x800m Relay"
    ('4 x 1600', 'TEMPLATE_4x1600m'), # Matches "4 x 1600m Relay"
    ('4x1600', 'TEMPLATE_4x1600m'),   # Matches "4x1600m Relay"
    ('3200', 'TEMPLATE_3200m'),    # Matches "3200 Meters", "3200m", etc.
    ('1600', 'TEMPLATE_1600m'),    # Matches "1600 Meters", "1600m", etc.
    ('800', 'TEMPLATE_800m'),      # Matches "800 Meters", "800m", etc.
]

XC_TEMPLATE = 'TEMPLATE_XC'


def get_template_for_event(event_name: str, sport: str, track_splits: str = 'finish') -> str:
    """Determine which template file to use for an event.

    Args:
        event_name: The event name from the .evt file (e.g., "Girls 800 Meters").
        sport: Either "track" or "xc".
        track_splits: For track, either "finish" (FL template) or "200m" (SPLITS template).

    Returns:
        Template filename without extension, or empty string if no template needed.
    """
    if sport == 'xc':
        return XC_TEMPLATE

    # Track events - check for matching patterns (relays first, then individual events)
    event_lower = event_name.lower()
    for pattern, template_base in TRACK_EVENT_TEMPLATES:
        if pattern.lower() in event_lower:
            # Append _FL or _SPLITS based on track split setting
            if track_splits == '200m':
                return f'{template_base}_SPLITS'
            else:
                return f'{template_base}_FL'

    return ''


def modify_evt_line_with_template(line: str, template_name: str) -> str:
    """Modify an event header line to include a template name in column 7.

    .evt format: event#,round,heat,name,wind,wind_units,TEMPLATE,capture_time,capture_duration,distance,...

    Args:
        line: The original event header line.
        template_name: The template filename to insert (without .evn extension).

    Returns:
        Modified line with template name in column 7.
    """
    parts = line.split(',')

    # Ensure we have at least 7 columns
    while len(parts) < 7:
        parts.append('')

    # Set column 7 (index 6) to the template name
    parts[6] = template_name

    return ','.join(parts)


def process_evt_file_with_templates(content: str, sport: str, track_splits: str = 'finish') -> tuple:
    """Process .evt file content and insert template names for matching events.

    Args:
        content: The .evt file content.
        sport: Either "track" or "xc".
        track_splits: For track, either "finish" (FL template) or "200m" (SPLITS template).

    Returns:
        Tuple of (modified_content, list of templates used, count of events modified, list of event details).
    """
    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    modified_lines = []
    templates_used = set()
    events_modified = 0
    events_with_templates = []  # List of event details for dropdown

    for line in lines:
        stripped = line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith(';'):
            modified_lines.append(line)
            continue

        # Check if this is an event header line (starts with a number)
        if stripped and stripped[0].isdigit():
            # This is an event header line
            parts = line.split(',')
            if len(parts) >= 4:
                event_num = parts[0].strip() if len(parts) > 0 else ''
                round_num = parts[1].strip() if len(parts) > 1 else ''
                heat_num = parts[2].strip() if len(parts) > 2 else ''
                event_name = parts[3].strip() if len(parts) > 3 else ''
                template = get_template_for_event(event_name, sport, track_splits)

                if template:
                    line = modify_evt_line_with_template(line, template)
                    templates_used.add(template)
                    events_modified += 1

                    # Store event details for dropdown
                    events_with_templates.append({
                        'event_num': event_num,
                        'round': round_num,
                        'heat': heat_num,
                        'name': event_name,
                        'template': template
                    })

        modified_lines.append(line)

    return '\n'.join(modified_lines), list(templates_used), events_modified, events_with_templates


def get_templates_directory() -> str:
    """Get the path to the templates directory."""
    # Templates are stored in the same directory as the middleware
    import pathlib
    return str(pathlib.Path(__file__).parent.parent / 'templates')


def copy_templates_to_directory(templates: list, target_dir: str) -> dict:
    """Copy template files to the target directory.

    Args:
        templates: List of template filenames (without .evn extension).
        target_dir: Target directory path.

    Returns:
        Dict with 'copied' (list of copied files) and 'errors' (list of error messages).
    """
    import shutil
    import pathlib

    templates_dir = get_templates_directory()
    result = {'copied': [], 'errors': []}

    for template in templates:
        src = pathlib.Path(templates_dir) / f'{template}.evn'
        dst = pathlib.Path(target_dir) / f'{template}.evn'

        try:
            if src.exists():
                shutil.copy2(src, dst)
                result['copied'].append(f'{template}.evn')
                logger.info(f"Copied template {template}.evn to {target_dir}")
            else:
                result['errors'].append(f"Template not found: {template}.evn")
                logger.warning(f"Template file not found: {src}")
        except Exception as e:
            result['errors'].append(f"Failed to copy {template}.evn: {str(e)}")
            logger.error(f"Error copying template {template}.evn: {e}")

    return result


@app.route('/api/browse-folder', methods=['POST'])
def api_browse_folder():
    """Open a folder browser dialog and return the selected path.

    This uses tkinter's folder dialog on Windows/Linux.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        # Create a hidden root window
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)  # Bring dialog to front

        # Open folder selection dialog
        folder_path = filedialog.askdirectory(
            title='Select .evt file directory',
            mustexist=True
        )

        root.destroy()

        if folder_path:
            return jsonify({'success': True, 'path': folder_path})
        else:
            return jsonify({'success': False, 'error': 'No folder selected'})

    except Exception as e:
        logger.error(f"Error opening folder browser: {e}")
        return jsonify({'success': False, 'error': str(e)})


def _autoload_evt(directory: str) -> Optional[str]:
    """Find the .evt in the meet folder, remember it in config, parse its
    heats for bib filtering. Returns the file content or None."""
    import pathlib
    if not _application:
        return None
    config = _application.config
    folder = start_list_dir(directory)
    name = config.evt_file if config.evt_file and \
        (pathlib.Path(folder) / config.evt_file).exists() else find_evt_file(folder)
    if not name:
        return None
    path = pathlib.Path(folder) / name
    content = path.read_text(encoding='utf-8', errors='replace')
    if config.evt_file != name or config.event_directory != directory:
        config.evt_file = name
        config.event_directory = directory
        try:
            _application.save_config()
        except Exception:
            logger.exception("Could not persist evt_file")
    with _event_data_lock:
        _event_data['heats'] = parse_all_heat_entries(content)
    with _athlete_lock:
        _athlete_data['directory'] = directory
    logger.info(f"Loaded .evt {path}")
    # Watch it from now on: when MeetPro rewrites it (late entries,
    # scratches, a renamed or added event) the panes follow by themselves
    with _evt_monitor_lock:
        fresh = (_evt_monitor_state['monitored_file'], _evt_monitor_state['monitored_dir']) != (name, folder)
        if fresh:
            _evt_monitor_state['config'] = None   # a legacy template run was for another file
        _evt_monitor_state['monitored_file'] = name
        _evt_monitor_state['monitored_dir'] = folder
        _evt_monitor_state['last_applied_timestamp'] = path.stat().st_mtime
    _apply_evt_to_panes(content, name, fresh=fresh)
    start_evt_monitor()
    return content


@app.route('/api/evt/races', methods=['GET'])
def api_evt_races():
    """Events/rounds/heats from the meet folder's .evt for the race picker,
    flagged with whether a race pane already exists for each."""
    if not _application:
        return jsonify({'success': False, 'error': 'Application not initialized'}), 503
    directory = _application.config.event_directory
    if not directory:
        return jsonify({'success': False, 'error': 'Load a meet folder first', 'races': []})
    try:
        content = _autoload_evt(directory)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not read .evt: {e}', 'races': []})
    if content is None:
        return jsonify({'success': False, 'error': f'No lynx.evt in {directory}', 'races': []})
    races = evt_race_list(content)
    mgr = _race_manager()
    existing = {}
    if mgr:
        with mgr._lock:
            for r in mgr.races.values():
                existing[(r.event, r.round, r.heat)] = r.key
    for r in races:
        r['pane_key'] = existing.get((r['event'], r['round'], r['heat']))
    return jsonify({'success': True, 'file': _application.config.evt_file,
                    'directory': directory, 'races': races})


@app.route('/api/evt/load', methods=['GET'])
def api_load_evt():
    """Load .evt file content from disk using persisted config.

    Returns the content of the evt file if it exists.
    """
    import pathlib

    if not _application:
        return jsonify({'success': False, 'error': 'Application not initialized'})

    config = _application.config
    if not config.event_directory or not config.evt_file:
        return jsonify({'success': False, 'error': 'No evt file configured'})

    evt_path = pathlib.Path(start_list_dir(config.event_directory)) / config.evt_file

    if not evt_path.exists():
        return jsonify({'success': False, 'error': f'File not found: {evt_path}'})

    try:
        with open(evt_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Also parse the heat entries for bib filtering
        global _event_data
        all_entries = parse_all_heat_entries(content)
        with _event_data_lock:
            _event_data['heats'] = all_entries

        # Set the meet directory for LIF saving (used by auto-save on event change)
        with _athlete_lock:
            _athlete_data['directory'] = config.event_directory

        # Start file monitoring
        with _evt_monitor_lock:
            _evt_monitor_state['monitored_file'] = config.evt_file
            _evt_monitor_state['monitored_dir'] = config.event_directory
            _evt_monitor_state['last_applied_timestamp'] = evt_path.stat().st_mtime

        start_evt_monitor()

        logger.info(f"Loaded evt file from {evt_path}")
        return jsonify({
            'success': True,
            'content': content,
            'filename': config.evt_file,
            'directory': config.event_directory,
        })
    except Exception as e:
        logger.error(f"Error loading evt file: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/evt/save', methods=['POST'])
def api_save_evt():
    """Process .evt file: insert templates and save to target directory.

    Body: {
        "filename": "event.evt",
        "content": "...evt file content...",
        "target_dir": "/path/to/evt/directory",
        "config": {
            "sport": "track" | "xc",
            "splits": 0|1|2 (xc) or "finish"|"200m" (track)
        }
    }

    This will:
    1. Process the .evt content and insert template names for matching events
    2. Copy the required template files to the target directory
    3. Save the modified .evt file to the target directory
    """
    logger.info("=== api_save_evt called ===")
    if not _application:
        logger.error("api_save_evt: Application not initialized")
        return jsonify({'error': 'Application not initialized'}), 503

    data = request.json
    logger.debug(f"api_save_evt: Received data keys: {list(data.keys()) if data else 'None'}")
    filename = data.get('filename', '')
    content = data.get('content', '')
    target_dir = data.get('target_dir', '')
    config = data.get('config', {})
    logger.info(f"api_save_evt: filename={filename}, target_dir={target_dir}, config={config}")
    logger.debug(f"api_save_evt: content length={len(content) if content else 0}")

    if not filename or not content:
        logger.error(f"api_save_evt: Missing required fields - filename={bool(filename)}, content={bool(content)}")
        return jsonify({'error': 'filename and content required'}), 400
    try:
        filename = _bare_name(filename, '.evt')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    if not target_dir:
        logger.error("api_save_evt: target_dir is required but not provided")
        return jsonify({'error': 'target_dir is required'}), 400

    # Validate target directory exists
    import pathlib
    target_path = pathlib.Path(target_dir)
    logger.info(f"api_save_evt: Validating target_path={target_path}, exists={target_path.exists()}, is_dir={target_path.is_dir() if target_path.exists() else 'N/A'}")
    if not target_path.exists() or not target_path.is_dir():
        logger.error(f"api_save_evt: Directory does not exist: {target_dir}")
        return jsonify({'error': f'Directory does not exist: {target_dir}'}), 400

    sport = config.get('sport', 'track')
    splits = config.get('splits')
    logger.info(f"api_save_evt: sport={sport}, splits={splits}")

    # Determine lap_mode and total_laps based on sport and split settings
    if sport == 'xc':
        num_splits = int(splits) if splits is not None else 0
        lap_mode = num_splits > 0
        total_laps = num_splits + 1
    else:
        if splits == '200m':
            lap_mode = True
            total_laps = 2
        else:
            lap_mode = False
            total_laps = 1

    # Update the middleware config
    _application.config.timing.lap_mode = lap_mode
    _application.config.timing.total_laps = total_laps
    _application.config.event_directory = target_dir
    _application.config.evt_file = filename
    _application.save_config()

    try:
        # Process the .evt file and insert template names
        # For track, splits is "finish" or "200m"; pass it to determine _FL vs _SPLITS templates
        track_splits = splits if sport == 'track' else 'finish'
        logger.info(f"api_save_evt: Processing .evt file with sport={sport}, track_splits={track_splits}")
        modified_content, templates_used, events_modified, events_with_templates = process_evt_file_with_templates(content, sport, track_splits)
        logger.info(f"api_save_evt: Processed - events_modified={events_modified}, templates_used={templates_used}")

        # Parse heat entries for bib filtering
        global _event_data
        all_entries = parse_all_heat_entries(modified_content)
        with _event_data_lock:
            _event_data['heats'] = all_entries

            # Refresh current heat info if one is already selected (from scoreboard)
            current_event = _event_data.get('current_event')
            if current_event and current_event in all_entries:
                _event_data['current_heat_info'] = all_entries[current_event]
        logger.info(f"api_save_evt: Parsed {len(all_entries)} heats with "
                   f"{sum(len(h.get('bib_lanes', {})) for h in all_entries.values())} lane assignments")
        if current_event and current_event in all_entries:
            logger.info(f"api_save_evt: Refreshed current heat info for event {current_event}")
        elif current_event:
            logger.warning(f"api_save_evt: Current event {current_event} not found in .evt file")

        # Copy template files to target directory
        copy_result = {'copied': [], 'errors': []}
        if templates_used:
            logger.info(f"api_save_evt: Copying {len(templates_used)} templates to {target_dir}")
            copy_result = copy_templates_to_directory(templates_used, target_dir)
            logger.info(f"api_save_evt: Copy result - copied={copy_result['copied']}, errors={copy_result['errors']}")

        # Save the modified .evt file
        evt_path = target_path / filename
        logger.info(f"api_save_evt: Writing modified .evt file to {evt_path}")
        with open(evt_path, 'w', encoding='utf-8') as f:
            f.write(modified_content)
        logger.info(f"api_save_evt: Successfully wrote {len(modified_content)} bytes to {evt_path}")

        logger.info(f"Saved modified .evt file to {evt_path}")
        logger.info(f"Events modified: {events_modified}, Templates copied: {copy_result['copied']}")

        # Set up file monitoring for auto-update
        with _evt_monitor_lock:
            _evt_monitor_state['last_applied_timestamp'] = evt_path.stat().st_mtime
            _evt_monitor_state['monitored_file'] = filename
            _evt_monitor_state['monitored_dir'] = target_dir
            _evt_monitor_state['config'] = config
            _evt_monitor_state['content'] = modified_content
            _evt_monitor_state['file_locked'] = False
            _evt_monitor_state['retry_mode'] = False

        # Start the monitor if not already running
        start_evt_monitor()
        logger.info(f"Started monitoring {filename} for changes")

        # Set the meet directory for LIF saving (used by auto-save on event change)
        with _athlete_lock:
            _athlete_data['directory'] = target_dir
        logger.info(f"Set meet directory to {target_dir} for LIF saving")

        # Also try to load the .ppl file from the same directory if present
        ppl_loaded = load_ppl_from_directory(target_dir)
        if ppl_loaded:
            logger.info(f"Auto-loaded .ppl file from {target_dir}")

        return jsonify({
            'success': True,
            'lap_mode': lap_mode,
            'total_laps': total_laps,
            'modified_content': modified_content,
            'events_modified': events_modified,
            'templates_used': templates_used,
            'templates_copied': copy_result['copied'],
            'template_errors': copy_result['errors'],
            'evt_path': str(evt_path),
            'events_with_templates': events_with_templates,
            'message': f'Processed {events_modified} events, copied {len(copy_result["copied"])} templates'
        })

    except Exception as e:
        logger.error(f"Error processing .evt file: {e}", exc_info=True)
        return jsonify({
            'error': f'Failed to process .evt file: {str(e)}',
            'success': False
        }), 400


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.debug('Web client connected')
    emit_status_update()


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.debug('Web client disconnected')


# ---------------------------------------------------------------- races


def _race_manager():
    if _application and _application.race_manager:
        return _application.race_manager
    return None


def _races_payload() -> dict:
    mgr = _race_manager()
    if not mgr:
        return {"enabled": False}
    payload = mgr.snapshot()
    payload["enabled"] = True
    payload["results"] = {k: mgr.results(int(k)) for k in payload["races"]}
    if _application is not None and hasattr(_application, "lif_dirs"):
        lynx_dir, rfid_dir = _application.lif_dirs()
        payload["lif_dirs"] = {"finishlynx": lynx_dir, "rfid": rfid_dir}
    svc = getattr(_application, 'meet_service', None) if _application is not None else None
    if svc is not None:
        try:
            payload["divisions"] = svc.pane_divisions()      # race tags (2026-10-05)
        except Exception:
            logger.exception("pane divisions failed")
        try:
            payload["issues"] = svc.pane_issues()            # flagged runners (2026-10-06)
        except Exception:
            logger.exception("pane issues failed")
    with _evt_monitor_lock:
        payload["evt_sync"] = _evt_monitor_state.get('last_sync')
    return payload


def emit_races_update():
    mgr = _race_manager()
    if mgr:
        socketio.emit('races_update', _races_payload())


def emit_meet_update(summary: Optional[dict] = None):
    """Push the meet summary (MEET-API.md) after a meet edit; consoles also
    re-fetch GET /api/meet on connect."""
    if summary is None:
        svc = getattr(_application, 'meet_service', None) if _application is not None else None
        summary = svc.summary() if svc is not None else {"active": False}
    socketio.emit('meet_update', summary)


def _parse_elapsed(value: str) -> float:
    """Race time -> seconds: '6:12.3', '1:02:03', '372', '372.5'."""
    parts = [p.strip() for p in value.strip().split(":")]
    if not value.strip() or len(parts) > 3 or any(p == "" for p in parts):
        raise ValueError(f"unrecognized race time: {value!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    if secs < 0:
        raise ValueError("race time cannot be negative")
    return secs


def _parse_clock(value: str) -> datetime:
    """Accept HH:MM:SS(.mmm) on today's date, or a full ISO timestamp."""
    value = value.strip()
    if not value:
        raise ValueError("enter a clock time (HH:MM:SS.mmm) or a race time (M:SS.s)")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"unrecognized time: {value!r}")
    sec = float(parts[2])
    return datetime.now().replace(
        hour=int(parts[0]), minute=int(parts[1]), second=int(sec),
        microsecond=int(round((sec % 1) * 1000)) * 1000)


@app.route('/races')
def races_page():
    """Alias kept for muscle memory; the panes live on the root console."""
    return _console_page()


@app.route('/api/races')
def api_races():
    return jsonify(_races_payload())


@app.route('/api/races/create', methods=['POST'])
def api_races_create():
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True)
    key = mgr.create_race(
        name=d.get("name", ""), event=int(d.get("event", 0)),
        gender=d.get("gender", ""), round_=int(d.get("round", 1)),
        heat=int(d.get("heat", 1)), distance=str(d.get("distance", "")),
        scorers=int(d.get("scorers", 5)), displacers=int(d.get("displacers", 2)),
    )
    # Races picked from the .evt carry its entries: the start list (a
    # finisher not on it waits for "Save to .lif" instead of going to MeetPro)
    if d.get("entries"):
        mgr.set_entries(key, [str(b) for b in d["entries"]])
    # Starts already heard today go into the new pane's Gun picker; an
    # explicit choice from the dialog arms it right away.
    if _application:
        _application.offer_recent_starts(key)
    if d.get("gun"):
        try:
            ts = _parse_clock(str(d["gun"]))
        except ValueError as e:
            return jsonify({"key": key, "error": f"race created, start not set: {e}"}), 200
        mgr.set_gun(key, ts, d.get("gun_source") or "picked at creation")
        if _application and hasattr(_application, "on_operator_gun"):
            _application.on_operator_gun(key)
    return jsonify({"key": key})


@app.route('/api/races/<int:key>/export.csv')
def api_race_export_csv(key):
    mgr = _race_manager()
    if not mgr or key not in mgr.races:
        return jsonify({"error": "no such race"}), 404
    name = re.sub(r'[^A-Za-z0-9_-]+', '_', mgr.races[key].name).strip('_') or f"race{key}"
    return Response(mgr.results_csv([key]), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={name}-results.csv'})


@app.route('/api/races/export.csv')
def api_meet_export_csv():
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    stamp = datetime.now().strftime('%Y%m%d')
    return Response(mgr.results_csv(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=meet-results-{stamp}.csv'})


@app.route('/api/races/<int:key>/delete', methods=['POST'])
def api_races_delete(key):
    """Drop a race pane; its reads move to the Held pane."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    name = mgr.races[key].name
    released = mgr.delete_race(key)
    logger.info(f"Race pane deleted: {name} ({released} reads back to held)")
    return jsonify({"ok": True, "released_reads": released})


def _meet_archive_dir(mgr) -> str:
    """closed-meets/<YYYYMMDD-HHMMSS> next to the race state file."""
    base = os.path.dirname(os.path.abspath(mgr.state_path or "races-state.json"))
    return os.path.join(base, "closed-meets", datetime.now().strftime("%Y%m%d-%H%M%S"))


def _results_folder_close():
    """(results folder, RFID folder) when Close meet archives into the
    FinishLynx results folder (2026-10-05), else None: preview, no folder
    set, no RFID folder in it yet, or a whole drive (never swept)."""
    if _application is None or not getattr(_application, "hardware_allowed", False) \
            or not hasattr(_application, "lif_dirs"):
        return None
    root, rfid = _application.lif_dirs()
    if not root or not os.path.isdir(root) or not os.path.isdir(rfid):
        return None
    root = os.path.abspath(root)
    if os.path.dirname(root) == root:
        return None
    return root, rfid


def _meet_folder_name() -> tuple:
    """(folder name, meet name): '<date> <meet name>' of the open meet,
    else today's date + 'Meet'."""
    from .meet_folder import folder_name
    svc = getattr(_application, 'meet_service', None) if _application is not None else None
    meet = getattr(svc, 'meet', None)
    if meet is not None:
        return folder_name(meet.meet.date or datetime.now().strftime("%Y-%m-%d"), meet.meet.name), meet.meet.name
    return folder_name(datetime.now().strftime("%Y-%m-%d"), "Meet"), "Meet"


def _close_middleware(archive_dir: str) -> dict:
    """Archive meet.json + results CSV + race state into archive_dir, then
    clear every pane and read, the live read log and dedup memory, and
    detach the meet. Raises OSError before anything is cleared."""
    mgr = _race_manager()
    # Meet mode (2026-10-03): meet/<id>/ (meet.json + pulls) goes into the
    # archive BEFORE anything is cleared; a failed copy aborts the close.
    svc = getattr(_application, 'meet_service', None) if _application is not None else None
    meet_copy = svc.archive_to(archive_dir) if svc is not None else None
    summary = mgr.close_meet(archive_dir)
    if _application:
        try:
            _application.clear_reads()
        except Exception:
            logger.exception("clear_reads during meet close failed")
    reset_validation_state()
    if meet_copy:
        summary["archived"].append(meet_copy)
        summary["meet_archived"] = meet_copy
        try:
            svc.detach()
        except Exception:
            logger.exception("Meet detach after close failed")
    return summary


@app.route('/api/meet/close/plan')
def api_meet_close_plan():
    """What Close meet will do, for its dialog: the meet folder it makes
    in the results folder (or the one an unfinished close left), what gets
    copied there, and whether FinishLynx is still open."""
    svc = getattr(_application, 'meet_service', None)
    if svc is not None and getattr(svc, 'file_only', False) and svc.meet is not None:
        return jsonify({"file_only": True, "folder": svc.archived_folder(svc.meet.meet.id)})
    target = _results_folder_close()
    if not target:
        return jsonify({"folder": None})
    from . import fl_window, meet_folder
    root, rfid = target
    try:
        pend = meet_folder.pending(root)
        p = meet_folder.plan(root, rfid)
    except OSError as e:
        return jsonify({"folder": None, "error": str(e)})
    svc = getattr(_application, 'meet_service', None)
    meet_id = svc.meet.meet.id if svc is not None and getattr(svc, 'meet', None) is not None else None
    again = None if pend else _reclose_folder(svc, meet_id, root)
    p.update(root=root, rfid=rfid, resume=bool(pend), again=bool(again),
             folder=pend or again or os.path.join(root, _meet_folder_name()[0]),
             finishlynx_running=fl_window.running())
    return jsonify(p)


@app.route('/api/meet/close', methods=['POST'])
def api_meet_close():
    """Close out the meet. With a FinishLynx results folder (2026-10-05):
    make '<date> <meet name>' in it, archive meet.json/results/race state
    into its Middleware folder, clear the console, then copy FinishLynx's
    files and the RFID folder in, check every copy and only then delete
    the originals (meet_folder.py). A close that failed part way is
    finished by calling this again. Without one (preview, no folder):
    archive to closed-meets/<date-time> as before."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"success": False, "error": "race manager not enabled"}), 400
    svc = getattr(_application, 'meet_service', None)
    if svc is not None and getattr(svc, 'file_only', False) and svc.meet is not None:
        return _close_file_only(svc)
    target = _results_folder_close()
    if target:
        return _close_into_results_folder(*target)
    archive_dir = _meet_archive_dir(mgr)
    try:
        summary = _close_middleware(archive_dir)
    except OSError as e:
        logger.exception("Meet close aborted: archive failed")
        return jsonify({"success": False, "error": f"archive failed, nothing cleared: {e}"}), 500
    # The merged .lif files MeetPro reads go too (copies kept in the
    # archive), so the next meet's events never pick up this meet's rows.
    summary["lif_wiped"] = 0
    if _application and getattr(_application, "hardware_allowed", False) \
            and hasattr(_application, "wipe_merged_lifs"):
        try:
            summary["lif_wiped"] = len(_application.wipe_merged_lifs(archive_dir))
        except OSError:
            logger.exception("Could not clear the RFID .lif folder")
    summary["success"] = True
    summary["archive_dir"] = archive_dir if summary["archived"] else None
    summary["message"] = (f"Meet closed: {summary['races']} race(s), {summary['reads']} read(s) cleared"
                          + (f", {summary['lif_wiped']} .lif file(s) removed from the RFID folder"
                             if summary["lif_wiped"] else "")
                          + (f"; archived to {archive_dir}" if summary["archived"] or summary["lif_wiped"] else ""))
    if summary["lif_wiped"]:
        summary["archive_dir"] = archive_dir
    return jsonify(summary)


def _close_file_only(svc):
    """Save & close for a meet opened without FinishLynx (2026-10-05): its
    meet file goes back into its meet folder; race panes, reads, the race
    state saved there and the results folder are not touched."""
    from .meet_service import MeetError
    name = svc.meet.meet.name or svc.meet.meet.id
    try:
        folder = svc.save_back()
        svc.detach()
    except MeetError as e:
        return jsonify({"success": False, "error": f"{e}; the meet is still open, nothing else changed"}), e.status
    msg = (f"{name}: meet file saved back into {folder}; nothing else changed" if folder
           else f"{name} closed (saved in this console; its meet folder is not there)")
    logger.info(msg)
    return jsonify({"success": True, "file_only": True, "message": msg, "archive_dir": folder,
                    "races": 0, "reads": 0, "archived": []})


def _close_into_results_folder(root: str, rfid: str):
    from . import fl_window, meet_folder
    if fl_window.running():
        return jsonify({"success": False, "error": "FinishLynx is still open. Close FinishLynx, "
                        "then Close meet again - nothing was changed."}), 409
    svc = getattr(_application, 'meet_service', None)
    meet_id = svc.meet.meet.id if svc is not None and svc.meet is not None else None
    try:
        folder = meet_folder.pending(root)
        if folder is None:
            folder = _reclose_folder(svc, meet_id, root)
            if folder is not None:
                marker = meet_folder.read_marker(folder)
                marker["state"] = "middleware"
                meet_folder.write_marker(folder, marker)
            else:
                name, meet_name = _meet_folder_name()
                folder = meet_folder.create(root, name, meet_name)
    except OSError as e:
        logger.exception("Meet close aborted: could not make the meet folder")
        return jsonify({"success": False, "error": f"could not make the meet folder, nothing cleared: {e}"}), 500
    marker = meet_folder.read_marker(folder) or {}
    summary = {"races": 0, "reads": 0, "archived": [], "resumed": marker.get("state") != "middleware"}
    if marker.get("state") == "middleware":
        try:
            summary = _close_middleware(os.path.join(folder, "Middleware"))
        except OSError as e:
            logger.exception("Meet close aborted: archive failed")
            return jsonify({"success": False, "error": f"archive failed, nothing cleared: {e}"}), 500
        summary["resumed"] = False
        # A retry must never close the next meet into this folder.
        marker["state"] = "copying"
        meet_folder.write_marker(folder, marker)
        if meet_id:
            try:
                svc.note_archive(meet_id, folder)
            except OSError:
                logger.exception("Could not note where the meet was closed to")
    summary["archive_dir"] = folder
    try:
        moved = meet_folder.copy_then_clear(root, rfid, folder)
    except (meet_folder.CloseError, OSError) as e:
        logger.exception(f"Meet close: moving files into {folder} did not finish")
        summary.update(success=False, cleared=True, error=(
            f"The race panes and reads are closed and archived in {os.path.join(folder, 'Middleware')}, "
            f"but moving the results folder did not finish: {e}.\n\nNo original was deleted unless its copy "
            f"was checked. Fix the problem, then click Close meet again to finish."))
        return jsonify(summary), 500
    summary["success"] = True
    summary["files_moved"] = moved["files"]
    summary["message"] = (("Finished the earlier close: " if summary["resumed"] else
                           f"Meet closed: {summary['races']} race(s), {summary['reads']} read(s) cleared; ")
                          + f"{moved['files']} file(s) copied, checked and cleared from the results folder "
                          f"into {folder}")
    logger.info(summary["message"])
    return jsonify(summary)


def _reclose_folder(svc, meet_id, root):
    """The meet folder a reopened meet came from (2026-10-05): closing it
    again saves there, not into a new "(2)" folder."""
    from . import meet_folder
    folder = svc.archived_folder(meet_id) if meet_id else None
    if folder and os.path.isdir(folder) and os.path.normcase(os.path.dirname(os.path.abspath(folder))) \
            == os.path.normcase(root):
        m = meet_folder.read_marker(folder)
        if m and m.get("state") == "done":
            return folder
    return None


def find_meet_folder(path: str):
    """The closed-meet folder holding `path` (the folder itself, or a file
    anywhere inside it, e.g. Middleware/meet/meet.json), else None."""
    from . import meet_folder
    p = os.path.abspath(path or "")
    for _ in range(4):
        if os.path.isdir(p) and meet_folder.is_meet_folder(p):
            return p
        p = os.path.dirname(p)
    return None


LOOSE_LISTED = 12


def _loose_text(loose: list) -> str:
    names = [rel for _, rel in loose]
    more = len(names) - LOOSE_LISTED
    return ", ".join(names[:LOOSE_LISTED]) + (f" and {more} more" if more > 0 else "")


def reopen_plan(folder: str) -> dict:
    """What opening a closed meet with FinishLynx would need (2026-10-05),
    for the Open dialog's choice: everything that would stop it, including
    every file already in the results / RFID folders (nothing is ever
    copied over them). The meet-file-only open needs only an empty console."""
    from . import fl_window, meet_folder
    mgr = _race_manager()
    loaded = bool(mgr is not None and (mgr.races or mgr.reads))
    target = _results_folder_close()
    marker = meet_folder.read_marker(folder) or {}
    files = 0
    try:
        files = sum(1 for n in os.listdir(folder)
                    if not meet_folder.is_marker(n) and os.path.isfile(os.path.join(folder, n)))
        sub = os.path.join(folder, "RFID")
        if os.path.isdir(sub):
            files += sum(1 for n in os.listdir(sub) if os.path.isfile(os.path.join(sub, n)))
    except OSError:
        pass
    plan = {"folder": folder, "files": files, "loaded": loaded,
            "finishlynx_running": fl_window.running(), "unfinished": marker.get("state") != "done",
            "results_folder": target[0] if target else None, "loose": []}
    if target:
        plan["loose"] = [rel for _, rel in meet_folder.sources(*target)]
    why = []
    if plan["unfinished"]:
        why.append("closing it did not finish: Close meet first")
    if loaded:
        why.append("race panes or reads are loaded: close the current meet first")
    if not target:
        why.append("no FinishLynx results folder is set (or this is a preview copy)")
    elif plan["loose"]:
        why.append(f"the results folder already has {len(plan['loose'])} file(s), which must not be "
                   "overwritten: close that meet first, or move them")
    if plan["finishlynx_running"]:
        why.append("FinishLynx is open: close it first")
    plan["finishlynx_ok"] = not why
    plan["finishlynx_why"] = why
    plan["file_only_ok"] = not loaded and not plan["unfinished"]
    return plan


def reopen_closed_meet(folder: str, file_only: bool = False) -> dict:
    """Reopen a closed meet from its meet folder (2026-10-05): its FinishLynx
    files and RFID folder are copied back into the results folder, its race
    state (panes, reads, places) is loaded and the meet is opened, so a
    missed result can be fixed in FinishLynx and the console as during the
    meet. Close meet then saves back into the same folder. Raises MeetError
    with nothing changed.

    file_only (user's ask, 2026-10-05): open only its meet file - nothing
    copied into the results folder, no race state, no start lists or panes
    (MeetService.file_only) - to print, re-send or edit names and bibs.
    Save & close writes the meet file back into the meet folder."""
    from . import fl_window, meet_folder
    from .meet_service import MeetError
    if file_only:
        marker = meet_folder.read_marker(folder)
        if not marker:
            raise MeetError(f"{folder} is not a closed meet folder", 404)
        if marker.get("state") != "done":
            raise MeetError("closing that meet did not finish: click Close meet to finish it first", 409)
        mgr = _race_manager()
        if mgr is not None and (mgr.races or mgr.reads):
            raise MeetError("race panes or reads are loaded: close the current meet first", 409)
        meet_dir = os.path.join(folder, "Middleware", "meet")
        if not os.path.isfile(os.path.join(meet_dir, "meet.json")):
            raise MeetError(f"no meet file in {meet_dir}", 404)
        svc = getattr(_application, 'meet_service', None)
        meet = svc.reopen_from(meet_dir, folder, file_only=True)
        logger.info(f"Opened {folder} without FinishLynx (meet file only)")
        return {"meet": meet, "folder": folder, "files": 0, "races": 0, "reads": 0, "file_only": True}
    target = _results_folder_close()
    if not target:
        raise MeetError("set the FinishLynx results folder first (not in preview): reopening "
                        "copies the meet's files back into it", 409)
    root, rfid = target
    marker = meet_folder.read_marker(folder)
    if not marker:
        raise MeetError(f"{folder} is not a closed meet folder", 404)
    if marker.get("state") != "done":
        raise MeetError("closing that meet did not finish: click Close meet to finish it first", 409)
    if fl_window.running():
        raise MeetError("FinishLynx is still open: close it, then open the meet again", 409)
    mgr = _race_manager()
    if mgr is None:
        raise MeetError("race manager not enabled", 400)
    if mgr.races or mgr.reads:
        raise MeetError("race panes or reads are loaded: close the current meet first", 409)
    loose = meet_folder.sources(root, rfid)
    if loose:
        raise MeetError(f"the results folder already has {len(loose)} file(s), which would be mixed "
                        f"with this meet's: {_loose_text(loose)}. Close the current meet first, or "
                        "move them", 409)
    middleware = os.path.join(folder, "Middleware")
    meet_dir = os.path.join(middleware, "meet")
    state = os.path.join(middleware, "races-state.json")
    if not os.path.isfile(os.path.join(meet_dir, "meet.json")):
        raise MeetError(f"no meet file in {meet_dir}", 404)
    try:
        restored = meet_folder.restore_files(folder, root, rfid)
    except meet_folder.CloseError as e:
        raise MeetError(f"could not copy the meet's files back, nothing changed: {e}", 500) from None
    try:
        summary = mgr.restore_state(state) if os.path.isfile(state) else {"races": 0, "reads": 0}
    except ValueError as e:
        for p in restored:
            try:
                os.remove(p)
            except OSError:
                pass
        raise MeetError(f"could not load the race state, nothing changed: {e}", 500) from None
    svc = getattr(_application, 'meet_service', None)
    meet = svc.reopen_from(meet_dir, folder)
    logger.info(f"Reopened {folder}: {len(restored)} file(s) back, "
                f"{summary['races']} race(s), {summary['reads']} read(s)")
    return {"meet": meet, "folder": folder, "files": len(restored), **summary}


@app.route('/api/races/<int:key>/status', methods=['POST'])
def api_races_status(key):
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    try:
        mgr.set_status(key, request.get_json(force=True)["status"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route('/api/races/<int:key>/checkin', methods=['POST'])
def api_races_checkin(key):
    """Open or close a race's check-in (2026-10-04). Body: {"open": true,
    "reader": "CHUTE", "close_in_min": 10, "take": false} or {"open": false}.
    409 when the reader is checking in another race (send take=true to move it)."""
    from .races import CheckinBusy
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True) or {}
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    if not d.get("open"):
        mgr.close_checkin(key)
        return jsonify({"ok": True})
    try:
        close_in = d.get("close_in_min")
        close_in = float(close_in) if close_in not in (None, "") else None
        if close_in is not None and not 0 < close_in <= 600:
            raise ValueError("close in must be 1-600 minutes")
        reader = mgr.open_checkin(key, d.get("reader"), close_in, take=bool(d.get("take")))
    except CheckinBusy as e:
        return jsonify({"error": str(e), "busy": {"reader": e.reader, "race": e.race_key,
                                                  "name": e.race_name}}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "reader": reader})


@app.route('/api/races/<int:key>/checkin-settings', methods=['POST'])
def api_races_checkin_settings(key):
    """A race's own check-in reader ("" = meet default) and schedule:
    {"reader": "CHUTE", "auto": null | "off" | {"open_before": 30, "close_before": 5}}
    (auto null = follow the meet default; close_before null = at the gun)."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    d = request.get_json(force=True) or {}
    try:
        mgr.set_checkin_settings(key, d.get("reader"), d["auto"] if "auto" in d else "keep")
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route('/api/races/checkin-default', methods=['POST'])
def api_races_checkin_default():
    """Meet-wide check-in: {"reader": "CHUTE", "schedule": null |
    {"open_before": 30, "close_before": 5}} - every race with a start time
    follows it unless it has its own."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True) or {}
    try:
        mgr.set_checkin_default(d.get("reader"), d["schedule"] if "schedule" in d else "keep")
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route('/api/races/<int:key>/gun', methods=['POST'])
def api_races_gun(key):
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True)
    if d.get("clock") == "now":
        ts = datetime.now()
    else:
        try:
            ts = _parse_clock(d["clock"])
        except (ValueError, KeyError) as e:
            return jsonify({"error": str(e)}), 400
    mgr.set_gun(key, ts, d.get("source", "manual"))
    if _application and hasattr(_application, "on_operator_gun"):
        _application.on_operator_gun(key)
    return jsonify({"ok": True})


@app.route('/api/races/<int:key>/chip_lif', methods=['POST'])
def api_races_chip_lif(key):
    """The pane's "Save to .lif": body {"bib": "1190", "on": true|false}.
    Puts (or takes back out) a finisher's chip time in the merged .lif when
    FinishLynx lists them without a time. Never automatic (user's rule
    2026-09-30: stray finish-mat reads must not become results)."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    d = request.get_json(force=True) or {}
    bib = str(d.get("bib", "")).strip()
    if not bib:
        return jsonify({"error": "bib required"}), 400
    on = mgr.set_chip_to_lif(key, bib, bool(d.get("on", True)))
    logger.info(f"Race {key}: bib {bib} chip finish {'released to' if on else 'taken out of'} the .lif by the operator")
    return jsonify({"ok": True, "on": on})


@app.route('/api/races/<int:key>/waive', methods=['POST'])
def api_races_waive(key):
    """"Publish without splits" in a pane's Needs-attention list: accept
    that an athlete has no read at a point. Body {"bib": "16",
    "point": "S2" | omitted for all their missing points, "undo": bool}."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    d = request.get_json(force=True) or {}
    bib = str(d.get("bib", "")).strip()
    if not bib:
        return jsonify({"error": "bib required"}), 400
    point = str(d["point"]).strip() if d.get("point") else None
    waived = mgr.waive_missing(key, bib, point, undo=bool(d.get("undo", False)))
    return jsonify({"ok": True, "bib": bib, "waived": waived})


@app.route('/api/races/<int:key>/remove_bib', methods=['POST'])
def api_races_remove_bib(key):
    """Remove an athlete from a race: ignores all their reads in it.
    Body: {"bib": "16"}."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True) or {}
    bib = str(d.get("bib", "")).strip()
    if not bib:
        return jsonify({"error": "bib required"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    n = mgr.remove_bib(key, bib)
    return jsonify({"ok": True, "removed_reads": n})


@app.route('/api/races/<int:key>/scoring', methods=['POST'])
def api_races_scoring(key):
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True)
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    kw = {}
    if "gender" in d:
        kw["gender"] = str(d.get("gender") or "")
    if "fastest_pace" in d:
        # "4:00" per mile, or seconds; blank = back to the default
        raw = str(d.get("fastest_pace") or "").strip()
        try:
            kw["fastest_pace"] = _parse_elapsed(raw) if raw else None
        except ValueError as e:
            return jsonify({"error": f"fastest pace: {e}"}), 400
        kw["keep_pace"] = False
    try:
        mgr.set_scoring(key, int(d["scorers"]), int(d["displacers"]), **kw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route('/api/races/<int:key>/add_read', methods=['POST'])
def api_races_add_read(key):
    """Body: {"bib", "point", "clock", "estimated": bool, "note": str}.
    estimated marks a computed split the operator accepted (see
    /estimate); it is flagged in the console and the CSV export."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    d = request.get_json(force=True)
    bib = str(d.get("bib", "")).strip()
    if not bib:
        return jsonify({"error": "bib required"}), 400
    # Either a clock time (HH:MM:SS.mmm) or a race time as a coach would
    # say it ("6:12.3", "1:02:03", or plain seconds), measured from the
    # pane's gun. A clock value with no hours part is treated as race time.
    clock = str(d.get("clock", "") or "").strip()
    elapsed = str(d.get("elapsed", "") or "").strip()
    if not elapsed and clock and clock.count(":") < 2:
        elapsed, clock = clock, ""
    try:
        if elapsed:
            gun = mgr.races[key].gun
            if gun is None:
                return jsonify({"error": "this race has no gun yet - enter a clock time instead"}), 400
            ts = gun + timedelta(seconds=_parse_elapsed(elapsed))
        else:
            ts = _parse_clock(clock)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    point = d.get("point", "FINISH")
    estimated = bool(d.get("estimated"))
    if estimated and point in mgr.finish_readers:
        return jsonify({"error": "a finish time cannot be marked estimated"}), 400
    rid = mgr.add_manual_read(key, bib, ts, point, estimated=estimated,
                              note=str(d.get("note", ""))[:200])
    return jsonify({"read_id": rid})


LIF_EXPORT_DIR = "lif-exports"      # subfolder of the meet directory


def lif_export_dir() -> str:
    """Where "Save .lif" writes: a lif-exports subfolder of the meet
    directory (the FinishLynx event folder), so the RFID files sit with the
    meet but never replace FinishLynx's own <EEE>-<R>-<HH>.lif there.
    Falls back to the middleware's folder when no meet directory is set."""
    base = ""
    if _application:
        base = str(getattr(_application.config, "event_directory", "") or "")
    if not base or not os.path.isdir(base):
        base = os.getcwd()
    return os.path.join(base, LIF_EXPORT_DIR)


@app.route('/api/races/<int:key>/save_lif', methods=['POST'])
def api_races_save_lif(key):
    """Save the pane's results as a .lif for the operator (pane footer
    button). Body: {"splits": true|false}. Writes <meet dir>/lif-exports/
    <EEE>-<R>-<HH>.lif - never FinishLynx's own file."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    d = request.get_json(silent=True) or {}
    splits = bool(d.get("splits", True))
    if not mgr.results(key)["finishers"]:
        return jsonify({"error": "no finishers yet - nothing to save"}), 400
    out = mgr.save_lif(key, lif_export_dir(), splits=splits)
    return jsonify(dict(out, ok=True))


def _uploaded_lif_text() -> tuple:
    """(text, file name) of the multipart 'file' field: a .lif from the
    operator's file picker, FinishLynx's ANSI (cp1252) or UTF-8 with a BOM."""
    f = request.files.get("file")
    if f is None or not f.filename:
        raise ValueError("no file chosen (multipart field 'file')")
    raw = f.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("that file is too big for a .lif")
    text = raw[3:].decode("utf-8", "replace") if raw.startswith(b"\xef\xbb\xbf") \
        else raw.decode("cp1252", "replace")
    return text, f.filename


@app.route('/api/races/<int:key>/import_lif', methods=['POST'])
def api_races_import_lif(key):
    """"Select .lif file" (2026-10-09): the picked file's results are the
    truth for this pane (RaceManager.import_lif)."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    try:
        text, name = _uploaded_lif_text()
        summary = mgr.import_lif(key, text, name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    logger.info(f"Race {key}: results from picked .lif {summary['name']} "
                f"({summary['timed']} timed, {summary['flagged']} DNF/DQ, "
                f"{len(summary['moved'])} moved)")
    if _application is not None and hasattr(_application, "refresh_merged_lifs"):
        _application.refresh_merged_lifs([key])
    return jsonify({"ok": True, "lif": summary})


@app.route('/api/races/<int:key>/clear_lif', methods=['POST'])
def api_races_clear_lif(key):
    """Stop using the picked .lif: back to the reads and FinishLynx's file."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    cleared = mgr.clear_lif(key)
    if cleared:
        logger.info(f"Race {key}: picked .lif cleared")
        if _application is not None and hasattr(_application, "refresh_merged_lifs"):
            _application.refresh_merged_lifs([key])
    return jsonify({"ok": True, "cleared": cleared})


@app.route('/api/races/<int:key>/crossings', methods=['POST'])
def api_races_crossings(key):
    """Reads before / Reads after per station for one race (2026-10-09).
    Body: {"crossings": {station: {"before": n, "after": m}}} - every
    station listed is set (0 / 0 = the default: one read, nothing else
    expected). Reads already heard are renumbered."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    d = request.get_json(force=True) or {}
    items = d.get("crossings")
    if not isinstance(items, dict) or not items:
        return jsonify({"error": "crossings: {station: {before, after}} expected"}), 400
    out = {}
    try:
        for station, c in items.items():
            c = c if isinstance(c, dict) else {}
            out = mgr.set_crossings(key, station, c.get("before", 0), c.get("after", 0))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if _application is not None and hasattr(_application, "refresh_merged_lifs"):
        _application.refresh_merged_lifs([key])
    return jsonify({"ok": True, "crossings": out})


@app.route('/api/races/<int:key>/lif')
def api_races_lif(key):
    """The pane's .lif as a download (the console's "Save as..."): the
    same merged layout MeetPro reads from the RFID folder. ?splits=0 for
    finish times only."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    splits = request.args.get("splits", "1") not in ("0", "false", "no")
    fl_text = None
    if _application is not None and hasattr(_application, "lif_dirs"):
        fl_text = mgr.finishlynx_lif(key, _application.lif_dirs()[0])
    content = mgr.lif_content(key, splits=splits, fl_text=fl_text)
    name = mgr.lif_name(mgr.races[key])
    return Response(content.encode("cp1252", "replace"), mimetype='text/plain',
                    headers={'Content-Disposition': f'attachment; filename={name}'})


@app.route('/api/races/<int:key>/estimate_missing', methods=['POST'])
def api_races_estimate_missing(key):
    """Add an estimated read for every finisher's missing expected split
    (the review pane's "Estimate all"). Returns what was added and what
    had no basis for an estimate."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    out = mgr.estimate_missing(key)
    return jsonify({"ok": True,
                    "added": [{"bib": b, "point": p} for b, p in out["added"]],
                    "failed": [{"bib": b, "point": p, "reason": r} for b, p, r in out["failed"]]})


@app.route('/api/races/<int:key>/estimate')
def api_races_estimate(key):
    """Suggested time for a missing split: ?bib=123&point=S2. Returns
    {"ok": false, "reason"} when there is no defensible basis."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"ok": False, "reason": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"ok": False, "reason": f"no race {key}"}), 404
    bib = request.args.get("bib", "").strip()
    point = request.args.get("point", "FINISH").strip()
    if not bib:
        return jsonify({"ok": False, "reason": "enter a bib"})
    return jsonify(mgr.estimate_split(key, bib, point))


@app.route('/api/races/move', methods=['POST'])
def api_races_move():
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True)
    mgr.move_bib(str(d["bib"]), int(d["from"]), int(d["to"]))
    return jsonify({"ok": True})


@app.route('/api/reads/<int:read_id>/ignore', methods=['POST'])
def api_reads_ignore(read_id):
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    mgr.ignore_read(read_id)
    return jsonify({"ok": True})


@app.route('/api/reads/<int:read_id>/assign', methods=['POST'])
def api_reads_assign(read_id):
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    mgr.assign_read(read_id, int(request.get_json(force=True)["race"]))
    # With finishlynx.feed "all" a split the panes kept out at ingest goes
    # to FinishLynx now that the operator has placed it (or a note says why not).
    note = None
    if _application and hasattr(_application, "send_restored_read"):
        try:
            note = _application.send_restored_read(read_id)
        except Exception:
            logger.exception("send_restored_read failed")
    return jsonify({"ok": True, "finishlynx": note})


@app.route('/api/reads/<int:read_id>/override', methods=['POST'])
def api_reads_override(read_id):
    """Edit a read's time. Body: {"clock": "17:30:59.7"} or {"elapsed":
    "26:33.36"} (a value with no hours part is a race time, from the gun of
    the read's race); empty restores the raw read. The value is the time
    the console shows: a finish-mat read's film offset is added back so the
    pane shows what was typed. A clock time without a date keeps the read's
    own day (2026-10-09: it went on today's date, so editing yesterday's
    race gave a time a day long)."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    read = mgr.reads.get(read_id)
    if read is None:
        return jsonify({"error": f"no read {read_id}"}), 404
    d = request.get_json(force=True) or {}
    clock = str(d.get("clock", "") or "").strip()
    elapsed = str(d.get("elapsed", "") or "").strip()
    if not elapsed and clock and clock.count(":") < 2:
        elapsed, clock = clock, ""
    if not (clock or elapsed):
        mgr.override_time(read_id, None, d.get("note", ""))   # restore the raw read
        return jsonify({"ok": True})
    key = read.race if read.race is not None else read.crossing_race
    race = mgr.races.get(key) if key is not None else None
    try:
        if elapsed:
            if race is None or race.gun is None:
                return jsonify({"error": "this read has no race with a gun - enter a clock time instead"}), 400
            shown = race.gun + timedelta(seconds=_parse_elapsed(elapsed))
        else:
            shown = _parse_clock(clock)
            if "T" not in clock and " " not in clock.strip():
                shown = datetime.combine(read.ts.date(), shown.time())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    corr = mgr.film_correction(read, key) if (mgr.is_finish_read(read) and not read.manual) else 0.0
    mgr.override_time(read_id, shown + timedelta(seconds=corr), d.get("note", ""))
    return jsonify({"ok": True})


@app.route('/api/races/<int:key>/finish_time', methods=['POST'])
def api_races_finish_time(key):
    """Edit a runner's finish time (the Results page, 2026-10-09). Body:
    {"bib", "time"}: a race time ("26:33.36", from the gun) or a clock
    time (the race's own day). Wins over a picked .lif."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    race = mgr.races[key]
    d = request.get_json(force=True) or {}
    bib = str(d.get("bib", "")).strip()
    value = str(d.get("time", "") or "").strip()
    if not bib or not value:
        return jsonify({"error": "bib and time are required"}), 400
    if race.gun is None:
        return jsonify({"error": "this race has no gun"}), 400
    try:
        if value.count(":") < 2:
            shown = race.gun + timedelta(seconds=_parse_elapsed(value))
        else:
            shown = _parse_clock(value)
            if "T" not in value and " " not in value:
                shown = datetime.combine(race.gun.date(), shown.time())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if shown <= race.gun:
        return jsonify({"error": "that is not after the gun"}), 400
    out = mgr.set_finish_time(key, bib, shown)
    return jsonify({"ok": True, **out})


@app.route('/api/races/<int:key>/revert_finish', methods=['POST'])
def api_races_revert_finish(key):
    """The pane's "M": a finish set by hand goes back to the read time
    (body {"bib"})."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    bib = str((request.get_json(force=True) or {}).get("bib", "")).strip()
    try:
        out = mgr.revert_finish(key, bib)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **out})


@app.route('/api/races/<int:key>/athlete/<bib>/reads')
def api_races_athlete_reads(key, bib):
    """Every read of one runner in one race, counted or not (the runner dialog)."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    if key not in mgr.races:
        return jsonify({"error": f"no race {key}"}), 404
    return jsonify({"reads": mgr.athlete_reads(key, str(bib))})


@app.route('/api/reads/<int:read_id>/use', methods=['POST'])
def api_reads_use(read_id):
    """"Use this read": it counts at its point in the race (body {"race"}),
    every other reader crossing there is ignored."""
    mgr = _race_manager()
    if not mgr:
        return jsonify({"error": "race manager not enabled"}), 400
    d = request.get_json(force=True) or {}
    try:
        key = int(d.get("race"))
    except (TypeError, ValueError):
        return jsonify({"error": "race missing"}), 400
    if read_id not in mgr.reads or key not in mgr.races:
        return jsonify({"error": "no such read or race"}), 404
    out = mgr.use_read(read_id, key)
    note = None
    if _application is not None and hasattr(_application, "send_restored_read"):
        try:
            note = _application.send_restored_read(read_id)
        except Exception:
            logger.exception("Sending a used read to FinishLynx failed")
    return jsonify({"ok": True, **out, "finishlynx": note})


# Meet management routes (docs/MEET-API.md), registered last so they see
# everything above (2026-10-03). The station package has no meet_api.
if importlib.util.find_spec(f"{__package__}.meet_api"):
    from .meet_api import bp as _meet_bp  # noqa: E402
    app.register_blueprint(_meet_bp)


def run_web_server(host: str = '0.0.0.0', port: int = 8080):
    """Run the web server."""
    logger.info(f'Starting web interface at http://{host}:{port}')
    socketio.run(app, host=host, port=port, debug=False, use_reloader=False, log_output=False, allow_unsafe_werkzeug=True)
