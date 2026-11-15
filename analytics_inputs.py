# SPDX-License-Identifier: AGPL-3.0-only
"""Compact snapshots of the inputs used by PAM analytics."""

import json

from sqlalchemy import text


# Bump when a change to the estimator requires a fresh baseline.
VERSION = 1
CHECKPOINT_TABLE = "pam_analytics_checkpoint"


def read_inputs(conn, thresholds):
    """Read fingerprints and counts inside the caller's read-only snapshot."""
    detections = conn.execute(text("""
        SELECT species_id, count(*) AS n, bit_xor(hash_record_extended(
            ROW(detection_id, recording_id, confidence), 871014210)) AS fingerprint
        FROM detections
        WHERE species_id IS NOT NULL
        GROUP BY species_id
    """))
    species = {
        str(row.species_id): [row.n, row.fingerprint]
        for row in detections
    }
    periods = {
        row.species_id: [row.start_month, row.end_month]
        for row in conn.execute(text("""
            SELECT species_id, start_month, end_month FROM species_monitoring_periods
        """))
    }
    for key, fingerprint in species.items():
        species_id = int(key)
        fingerprint.extend([
            thresholds.get(species_id, thresholds.default_factory()),
            periods.get(species_id, [1, 12]),
        ])

    shared = conn.execute(text("""
        SELECT ARRAY[
            (SELECT count(*) FROM recordings),
            (SELECT bit_xor(hash_record_extended(ROW(r.recording_id, r.datetime_start,
                                                    r.location_id, l.location_id), 871014210))
               FROM recordings r LEFT JOIN locations l USING (location_id)),
            (SELECT count(*) FROM location_biotopes),
            (SELECT bit_xor(hash_record_extended(ROW(location_id, biotope_id), 871014210))
               FROM location_biotopes),
            (SELECT count(*) FROM location_institutions),
            (SELECT bit_xor(hash_record_extended(ROW(location_id, institution_id), 871014210))
               FROM location_institutions)
        ]
    """)).scalar_one()
    return {"version": VERSION, "shared": shared, "species": species}


def select_species(current, previous, *, force=False):
    """Return species IDs and current counts, including zero for deleted inputs."""
    species = current["species"]
    old_species = previous["species"] if previous else {}
    all_ids = species.keys() | old_species.keys()
    if (
        not force and previous
        and previous["version"] == current["version"]
        and previous["shared"] == current["shared"]
    ):
        all_ids = {key for key in all_ids if species.get(key) != old_species.get(key)}
    return {key: species[str(key)][0] if str(key) in species else 0 for key in sorted(map(int, all_ids))}


def read_checkpoint(conn):
    if not conn.scalar(text("SELECT to_regclass(:table)"), {"table": CHECKPOINT_TABLE}):
        return None
    return conn.scalar(text("SELECT snapshot FROM pam_analytics_checkpoint WHERE id = 1"))


def save_checkpoint(conn, snapshot):
    conn.execute(text("""
        INSERT INTO pam_analytics_checkpoint (id, snapshot)
        VALUES (1, CAST(:snapshot AS jsonb))
        ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot
    """), {"snapshot": json.dumps(snapshot, allow_nan=False)})
