// SPDX-License-Identifier: AGPL-3.0-only
//
// Local export for the sample-upload page (destination = local folder): the
// reverse of the upload. The server plans which detections (or whole
// recordings) to take, the browser finds them in the local archive exactly as
// for an upload, and this file writes them into an output folder the operator
// picked, one subfolder per species (Latin name). Nothing reaches the server.
//
// Two steps by design, so terabytes are never copied by accident:
//   summarize()  counts per species what would be written and how big it is;
//   run()        writes, only after the page has shown that summary and the
//                operator confirmed it.
//
// Re-running is safe: a file already in the output folder (same name; for
// whole files also the same size) is skipped, and manifest.csv is merged, so
// an interrupted export continues where it stopped.
//
// No Jinja here: callers pass labels and render counts themselves.

(function (global) {
    'use strict';

    const MANIFEST = 'manifest.csv';
    const MANIFEST_COLUMNS = [
        'output_file', 'species', 'unit', 'detection_id', 'recording_id',
        'location_id', 'location', 'recording_file', 'recorded_date',
        'recorded_time', 'start_s', 'end_s', 'confidence', 'model_id',
        'verification_state', 'consensus', 'votes', 'positive_votes',
        'segment_start_s', 'segment_duration_s'
    ];
    // Output of a cut is always 16-bit PCM WAV; FLAC/MP3 sources have no cheap
    // header to read, so their size is estimated at 48 kHz mono.
    const FALLBACK_BYTES_PER_SECOND = 48000 * 2;

    // Mirrors pam_segment_sampling._safe_species_dirname, so a local export and
    // the server's ZIP layout name a species folder the same way.
    function speciesDir(name) {
        const clean = String(name || 'unknown').trim().replace(/[^\w\-. ]+/g, '_');
        return clean || 'unknown';
    }

    function segmentName(seg) {
        return seg.segment_filename.replace(/\.(flac|mp3)$/i, '.wav');
    }

    function absent(error) {
        return error.name === 'NotFoundError' || error.name === 'TypeMismatchError';
    }

    async function existingSize(dir, name) {
        try {
            return (await (await dir.getFileHandle(name)).getFile()).size;
        } catch (error) {
            if (absent(error)) return null;
            throw error;
        }
    }

    // The output folder must not be the archive or inside it: the next archive
    // search would index the exported copies as recordings.
    async function outputInsideArchive(outDir, archiveRoot) {
        if (!outDir || !archiveRoot) return false;
        if (await outDir.isSameEntry(archiveRoot)) return true;
        return (await archiveRoot.resolve(outDir)) !== null;
    }

    // One entry per output file. A whole-file export of two species that both
    // sang in one recording writes it into both species folders; within one
    // species the same recording is written once.
    function outputItems(plan, unit, fileFor) {
        const items = [];
        const seen = new Set();
        const names = new Map();      // species -> Map(name -> file key)
        for (const seg of plan) {
            const species = seg.species_name || '';
            const file = fileFor(seg);
            const fileKey = global.PamRecordingIndex
                ? global.PamRecordingIndex.key(seg)
                : String(seg.recording_filename || '').toLowerCase();
            let name;
            if (unit === 'files') {
                const id = species + '\u0000' + fileKey;
                if (seen.has(id)) continue;
                seen.add(id);
                name = file ? file.name : String(seg.recording_filename || '');
            } else {
                name = segmentName(seg);
            }
            if (!names.has(species)) names.set(species, new Map());
            const used = names.get(species);
            // Same basename from two locations: keep both, tag the second.
            if (used.has(name) && used.get(name) !== fileKey) {
                const dot = name.lastIndexOf('.');
                name = (dot > 0 ? name.slice(0, dot) : name) + '__loc' + seg.location_id
                    + (dot > 0 ? name.slice(dot) : '');
            }
            used.set(name, fileKey);
            items.push({ seg, species, file, name });
        }
        return items;
    }

    async function bytesPerSecond(file, cache) {
        if (cache.has(file)) return cache.get(file);
        let rate = FALLBACK_BYTES_PER_SECOND;
        if (/\.wav$/i.test(file.name)) {
            try {
                const header = global.PamSegmentCutter.parseWavHeader(
                    new DataView(await file.slice(0, 65536).arrayBuffer()));
                if (header) rate = header.fmt.sampleRate * header.fmt.numChannels * 2;
            } catch (error) { /* keep the fallback */ }
        }
        cache.set(file, rate);
        return rate;
    }

    // Step 1: per species, how many files would be written and how big. Only
    // headers are read (and only for a few recordings per species), so this is
    // cheap even for a large plan.
    async function summarize({ plan, unit, fileFor, duration }) {
        const bySpecies = new Map();
        const rates = new Map();
        for (const item of outputItems(plan, unit, fileFor)) {
            if (!bySpecies.has(item.species)) {
                bySpecies.set(item.species, {
                    species: item.species, folder: speciesDir(item.species),
                    planned: 0, found: 0, missing: 0, bytes: 0, estimated: unit !== 'files',
                    sampledRates: []
                });
            }
            const row = bySpecies.get(item.species);
            row.planned++;
            if (!item.file) { row.missing++; continue; }
            row.found++;
            if (unit === 'files') {
                row.bytes += item.file.size;
            } else if (row.sampledRates.length < 20) {
                row.sampledRates.push(await bytesPerSecond(item.file, rates));
            }
        }
        const rows = Array.from(bySpecies.values());
        for (const row of rows) {
            if (unit !== 'files') {
                const rate = row.sampledRates.length
                    ? row.sampledRates.reduce((a, b) => a + b, 0) / row.sampledRates.length
                    : FALLBACK_BYTES_PER_SECOND;
                row.bytes = Math.round(row.found * (44 + rate * (duration || 5)));
            }
            delete row.sampledRates;
        }
        rows.sort((a, b) => a.species.localeCompare(b.species));
        return {
            rows,
            planned: rows.reduce((n, r) => n + r.planned, 0),
            found: rows.reduce((n, r) => n + r.found, 0),
            missing: rows.reduce((n, r) => n + r.missing, 0),
            bytes: rows.reduce((n, r) => n + r.bytes, 0)
        };
    }

    // ── manifest.csv ─────────────────────────────────────────────────────────
    function csvCell(value) {
        const text = value === null || value === undefined ? '' : String(value);
        return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    }

    function parseCsv(text) {
        const rows = [];
        let row = [], cell = '', quoted = false;
        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (quoted) {
                if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
                else if (ch === '"') quoted = false;
                else cell += ch;
            } else if (ch === '"') quoted = true;
            else if (ch === ',') { row.push(cell); cell = ''; }
            else if (ch === '\n') { row.push(cell.replace(/\r$/, '')); rows.push(row); row = []; cell = ''; }
            else cell += ch;
        }
        if (cell || row.length) { row.push(cell); rows.push(row); }
        return rows;
    }

    function manifestRow(item, unit, duration, shift) {
        const seg = item.seg;
        const segmentStart = unit === 'files' ? ''
            : Math.max(0, (seg.start_s || 0) + (shift || 0));
        return {
            output_file: item.name, species: item.species, unit,
            detection_id: seg.detection_id, recording_id: seg.recording_id,
            location_id: seg.location_id, location: seg.location_name,
            recording_file: item.file ? item.file.name : seg.recording_filename,
            recorded_date: seg.recorded_date, recorded_time: seg.recorded_time,
            start_s: seg.start_s, end_s: seg.end_s, confidence: seg.confidence,
            model_id: seg.model_id, verification_state: seg.verification_state,
            consensus: seg.consensus, votes: seg.votes, positive_votes: seg.positive_votes,
            segment_start_s: segmentStart,
            segment_duration_s: unit === 'files' ? '' : (duration || 5)
        };
    }

    // Merge with what an earlier run wrote, keyed by output_file, so resuming
    // or widening an export keeps every row.
    async function writeManifest(dir, rows) {
        const merged = new Map();
        try {
            const text = await (await (await dir.getFileHandle(MANIFEST)).getFile()).text();
            const [header, ...body] = parseCsv(text);
            if (header && header[0] === 'output_file') {
                for (const cells of body) {
                    if (!cells.length || !cells[0]) continue;
                    const old = {};
                    header.forEach((column, i) => { old[column] = cells[i]; });
                    merged.set(old.output_file, old);
                }
            }
        } catch (error) {
            if (!absent(error)) throw error;
        }
        rows.forEach(row => merged.set(row.output_file, row));
        const lines = [MANIFEST_COLUMNS.join(',')];
        for (const row of merged.values()) {
            lines.push(MANIFEST_COLUMNS.map(column => csvCell(row[column])).join(','));
        }
        const handle = await dir.getFileHandle(MANIFEST, { create: true });
        const writer = await handle.createWritable();
        try {
            await writer.write(lines.join('\r\n') + '\r\n');
            await writer.close();
        } catch (error) {
            await writer.abort().catch(() => {});
            throw error;
        }
    }

    // ── step 2: write ────────────────────────────────────────────────────────
    // outcome.status: 'saved' | 'exists' | 'nofile' | 'error'
    async function writeOne(item, dir, unit, duration, shift) {
        if (!item.file) return { status: 'nofile' };
        try {
            const size = await existingSize(dir, item.name);
            if (size !== null && (unit === 'files' ? size === item.file.size : size > 44)) {
                return { status: 'exists' };
            }
            const blob = unit === 'files' ? null
                : await global.PamSegmentCutter.cut(item.file, item.seg, duration, shift);
            if (unit !== 'files' && !blob) return { status: 'nofile' };
            // createWritable writes to a swap file and commits on close, so an
            // interrupted copy never leaves a half-written file behind.
            const handle = await dir.getFileHandle(item.name, { create: true });
            const writer = await handle.createWritable();
            try {
                if (unit === 'files') {
                    await item.file.stream().pipeTo(writer);   // closes the writer
                } else {
                    await writer.write(blob);
                    await writer.close();
                }
            } catch (error) {
                await writer.abort().catch(() => {});
                throw error;
            }
            return { status: 'saved' };
        } catch (error) {
            return { status: 'error', error: error.message || String(error) };
        }
    }

    async function run({ plan, unit, outDir, fileFor, duration, shift, concurrency, onResult }) {
        const items = outputItems(plan, unit, fileFor);
        const total = items.length;
        const dirs = new Map();
        const manifests = new Map();
        const folder = species => {
            if (!dirs.has(species)) {
                dirs.set(species, outDir.getDirectoryHandle(speciesDir(species), { create: true }));
            }
            return dirs.get(species);
        };
        const queue = items.slice();
        let done = 0;
        // Whole files are big sequential reads: two at a time keep a disk busy
        // without thrashing it. Cuts are small and CPU-bound.
        const workers = [];
        for (let i = 0; i < (concurrency || (unit === 'files' ? 2 : 4)); i++) {
            workers.push((async () => {
                while (queue.length) {
                    const item = queue.shift();
                    let outcome;
                    try {
                        outcome = await writeOne(item, await folder(item.species), unit, duration, shift);
                    } catch (error) {
                        outcome = { status: 'error', error: error.message || String(error) };
                    }
                    if (outcome.status === 'saved' || outcome.status === 'exists') {
                        if (!manifests.has(item.species)) manifests.set(item.species, []);
                        manifests.get(item.species).push(manifestRow(item, unit, duration, shift));
                    }
                    done++;
                    if (onResult) onResult(item, outcome, done, total);
                }
            })());
        }
        await Promise.all(workers);
        const manifestErrors = [];
        for (const [species, rows] of manifests) {
            try {
                await writeManifest(await folder(species), rows);
            } catch (error) {
                manifestErrors.push(species + ': ' + (error.message || error));
            }
        }
        return { done, total, manifestErrors };
    }

    global.PamLocalExport = {
        supported: typeof global.showDirectoryPicker === 'function',
        MANIFEST,
        MANIFEST_COLUMNS,
        speciesDir,
        outputItems,
        outputInsideArchive,
        summarize,
        run,
        // exposed for the harness test
        parseCsv,
        csvCell
    };
})(window);
