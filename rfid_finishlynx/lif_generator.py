"""Lynx Interchange Format (LIF) file generation.

This module generates properly formatted LIF files for FinishLynx import.
Format based on FinishLynx LIF specification.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def generate_lif(
    event_details: Optional[Dict],
    participant_data: Dict[str, Dict],
    reads: List[Dict],
    start_time: Optional[str] = None,
    event_num_override: Optional[int] = None,
    round_num_override: Optional[int] = None,
    heat_num_override: Optional[int] = None,
) -> str:
    """Generate LIF file content.

    Args:
        event_details: Dict with event_num, round, heat_num, event_name, distance, etc.
        participant_data: Dict mapping bib -> athlete info (last_name, first_name, school, gender)
        reads: List of read dicts with bib, timestamp, time, identifier, athlete
        start_time: Optional race start time (HH:MM:SS.mmm) for header
        event_num_override: Override event number (from scoreboard)
        round_num_override: Override round number (from scoreboard)
        heat_num_override: Override heat number (from scoreboard)

    Returns:
        LIF file content as string with CRLF line endings
    """
    lines = []

    # Use override values if provided, otherwise fall back to event_details
    if event_num_override is not None:
        event_num = str(event_num_override)
    elif event_details:
        event_num = str(event_details.get('event_num', '1'))
    else:
        event_num = '1'

    if round_num_override is not None:
        round_num = str(round_num_override)
    elif event_details:
        round_num = str(event_details.get('round', '1'))
    else:
        round_num = '1'

    if heat_num_override is not None:
        heat_num = str(heat_num_override)
    elif event_details:
        heat_num = str(event_details.get('heat_num', '1'))
    else:
        heat_num = '1'

    event_name = event_details.get('event_name', '') if event_details else ''
    distance = str(event_details.get('distance', '')) if event_details else ''

    # Determine start time from first read's TOD if not provided
    first_timestamp = None
    if reads:
        first_timestamp = reads[0].get('timestamp')
        if not start_time and first_timestamp:
            start_time = first_timestamp.strftime('%H:%M:%S.') + f'{first_timestamp.microsecond // 1000:03d}'

    # Build header line (11 comma-separated fields to match FinishLynx format)
    # Event#, Round, Heat, EventName, Wind, WindUnit, Template, CaptureTime, CaptureDuration, Distance, StartTime
    header_fields = [
        event_num,
        round_num,
        heat_num,
        event_name,
        '',  # Wind
        '',  # Wind unit
        '',  # Template
        '',  # Capture time
        '',  # Capture duration
        distance,
        start_time or '',  # Start time (TOD)
    ]
    lines.append(','.join(header_fields))

    # Get bib-to-lane mapping from event details
    bib_lanes = event_details.get('bib_lanes', {}) if event_details else {}

    # Group reads by bib, keeping ALL reads for splits
    bib_data = {}
    for read in reads:
        bib = read.get('bib', '')
        if not bib:
            continue

        time_str = read.get('time', '')  # TOD
        timestamp = read.get('timestamp')
        lane = bib_lanes.get(bib, '')  # Look up lane from .evt file
        athlete = read.get('athlete') or participant_data.get(bib, {})

        if bib not in bib_data:
            bib_data[bib] = {
                'bib': bib,
                'lane': lane,  # Lane from .evt file
                'athlete': athlete,
                'splits': [],  # All reads for this bib
            }

        # Calculate elapsed time from first timestamp
        elapsed_seconds = 0.0
        if timestamp and first_timestamp:
            elapsed_seconds = (timestamp - first_timestamp).total_seconds()

        # Add this read to splits list
        bib_data[bib]['splits'].append({
            'time': time_str,
            'timestamp': timestamp,
            'elapsed': elapsed_seconds,
        })

    # Sort by finish time (last split's elapsed time) to determine place order
    def get_finish_time(data):
        splits = data.get('splits', [])
        if splits:
            return splits[-1].get('elapsed', float('inf'))
        return float('inf')

    sorted_bibs = sorted(bib_data.values(), key=get_finish_time)

    # Get first finisher's time for gap calculation
    first_finish_time = get_finish_time(sorted_bibs[0]) if sorted_bibs else 0

    # Build result lines (19 comma-separated fields per line)
    for place, data in enumerate(sorted_bibs, 1):
        bib = data['bib']
        lane = data.get('lane', '')  # Lane from .evt file
        athlete = data.get('athlete', {})
        splits = data.get('splits', [])

        # Get finish time (last split)
        finish_time = 0
        finish_tod = ''
        if splits:
            finish_time = splits[-1].get('elapsed', 0)
            finish_tod = splits[-1].get('time', '')

        # Format finish time
        finish_time_str = _format_race_time(finish_time) if finish_time > 0 else ''

        # Build splits string: "time1 (delta1),time2 (delta2),..."
        splits_parts = []
        prev_time = 0
        for split in splits:
            split_time = split.get('elapsed', 0)
            # Include all splits, even those at elapsed time 0 (first finisher's first read)
            if split_time >= 0:
                split_str = _format_race_time(split_time)
                delta = split_time - prev_time
                delta_str = _format_race_time(delta)
                splits_parts.append(f"{split_str} ({delta_str})")
                prev_time = split_time
        # Wrap in quotes since it contains commas
        splits_display = f'"{",".join(splits_parts)}"' if splits_parts else ''

        # Calculate gap from leader
        gap_str = ''
        if place > 1 and finish_time > 0 and first_finish_time > 0:
            gap = finish_time - first_finish_time
            gap_str = _format_race_time(gap)

        # Look up athlete info
        last_name = athlete.get('last_name', '')
        first_name = athlete.get('first_name', '')
        school = athlete.get('school', '')
        gender = athlete.get('gender', '')

        # Calculate speed if we have distance and time
        speed_str = ''
        if distance and finish_time > 0:
            try:
                dist_m = float(distance)
                speed = dist_m / finish_time  # m/s
                speed_str = f"{speed:.2f}"
            except (ValueError, ZeroDivisionError):
                pass

        # Build result line (19 fields matching FinishLynx format)
        # Place, Bib, Lane, LastName, FirstName, Affiliation, Time, License,
        # SplitTime, ReacTime, SplitsDisplay, TOD, Gender, User1, User2,
        # GapFromLeader, CumulativeTime, Speed, FinalTime
        result_fields = [
            str(place),
            bib,
            lane,  # Lane from .evt file
            last_name,
            first_name,
            school,
            finish_time_str,  # Finish time
            '',  # License
            finish_time_str,  # Split time (same as finish for now)
            '',  # ReacTime
            splits_display,  # All splits with deltas
            finish_tod,  # Time of day of finish
            gender,
            '',  # User 1
            '',  # User 2
            gap_str,  # Gap from leader
            finish_time_str,  # Cumulative time
            speed_str,  # Speed (m/s)
            finish_time_str,  # Final time
        ]
        lines.append(','.join(result_fields))

    return '\r\n'.join(lines) + '\r\n'


def generate_filename(event_num: int, round_num: int, heat_num: int, timestamp: Optional[datetime] = None) -> str:
    """Generate a LIF filename with timestamp to prevent overwrites.

    Args:
        event_num: Event number
        round_num: Round number
        heat_num: Heat number
        timestamp: Optional timestamp (defaults to now)

    Returns:
        Filename in format NNN-R-HH-HHMMSS.lif (e.g., 016-1-02-143052.lif)
    """
    if timestamp is None:
        timestamp = datetime.now()

    # Format: 3-digit event, 1-digit round, 2-digit heat, timestamp (HHMMSS)
    time_suffix = timestamp.strftime("%H%M%S")
    return f"{event_num:03d}-{round_num}-{heat_num:02d}-{time_suffix}.lif"


def _format_race_time(seconds: float) -> str:
    """Format seconds to race time string (M:SS.sss or H:MM:SS.sss).

    Args:
        seconds: Time in seconds

    Returns:
        Formatted time string
    """
    if seconds < 0:
        seconds = 0

    total_ms = int(seconds * 1000)
    total_seconds = total_ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    millis = total_ms % 1000

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"
    else:
        return f"{minutes}:{secs:02d}.{millis:03d}"
