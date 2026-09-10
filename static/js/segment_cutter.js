// SPDX-License-Identifier: AGPL-3.0-only
//
// Browser-side segment cutting, shared by the two pages that produce segments
// for verification:
//
//   * pam_sample_upload.html      — a confidence-stratified SAMPLE over a
//     period, for measuring a model's precision;
//   * pam_segment_window.html     — exactly the detections of ONE time window,
//     opened from the co-occurrence map to confirm one co-occurrence.
//
// The two differ only in how the plan is chosen. A plan is a list of dicts as
// produced by pam_segment_sampling.run_stratified_sample / plan_window_segments,
// and everything below operates on that shape, so neither page owns this code.
//
// No Jinja here on purpose: this is a static file, so it cannot carry
// translated strings. Callers pass their own labels / log formatting through
// the option hooks.

(function (global) {
    'use strict';

    const AUDIO_EXT = /\.(wav|flac|mp3)$/i;

// ── audio cutting ────────────────────────────────────────────────────────
// Encode Float32 (or Int16) PCM channels into a 16-bit PCM WAV Blob.
function encodeWav(channels, sampleRate) {
    const numCh = channels.length;
    const numFrames = channels[0].length;
    const bytesPerSample = 2;
    const dataSize = numFrames * numCh * bytesPerSample;
    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);
    const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    writeStr(0, 'RIFF'); view.setUint32(4, 36 + dataSize, true); writeStr(8, 'WAVE');
    writeStr(12, 'fmt '); view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);                       // PCM
    view.setUint16(22, numCh, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * numCh * bytesPerSample, true);
    view.setUint16(32, numCh * bytesPerSample, true);
    view.setUint16(34, 16, true);
    writeStr(36, 'data'); view.setUint32(40, dataSize, true);
    let off = 44;
    for (let i = 0; i < numFrames; i++) {
        for (let c = 0; c < numCh; c++) {
            let s = Math.max(-1, Math.min(1, channels[c][i]));
            view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
            off += 2;
        }
    }
    return new Blob([view], { type: 'audio/wav' });
}

// Locate fmt/data chunks in a WAV header (reads only the header bytes).
function parseWavHeader(dv) {
    if (dv.getUint32(0, false) !== 0x52494646 /*RIFF*/) return null;
    let off = 12, fmt = null, dataOffset = null, dataSize = null;
    while (off + 8 <= dv.byteLength) {
        const id = String.fromCharCode(dv.getUint8(off), dv.getUint8(off+1), dv.getUint8(off+2), dv.getUint8(off+3));
        const size = dv.getUint32(off + 4, true);
        if (id === 'fmt ') {
            fmt = {
                audioFormat: dv.getUint16(off + 8, true),
                numChannels: dv.getUint16(off + 10, true),
                sampleRate: dv.getUint32(off + 12, true),
                bitsPerSample: dv.getUint16(off + 22, true),
            };
        } else if (id === 'data') {
            dataOffset = off + 8;
            dataSize = size;
            break;
        }
        off += 8 + size + (size & 1);
    }
    if (!fmt || dataOffset === null) return null;
    return { fmt, dataOffset, dataSize };
}

// Fast path for WAV: byte-slice the requested window without decoding the
// whole file. Returns a WAV Blob or null (unsupported → caller falls back).
async function cutWavByBytes(file, startSec, durSec) {
    const headBuf = await file.slice(0, 65536).arrayBuffer();
    const hdr = parseWavHeader(new DataView(headBuf));
    if (!hdr) return null;
    const { fmt, dataOffset, dataSize } = hdr;
    if (fmt.audioFormat !== 1 || (fmt.bitsPerSample % 8) !== 0) return null; // PCM int only
    const bytesPerFrame = fmt.numChannels * (fmt.bitsPerSample / 8);
    const totalFrames = Math.floor((dataSize || (file.size - dataOffset)) / bytesPerFrame);
    let startFrame = Math.max(0, Math.floor(startSec * fmt.sampleRate));
    if (startFrame >= totalFrames) return null;
    let numFrames = Math.floor(durSec * fmt.sampleRate);
    if (startFrame + numFrames > totalFrames) numFrames = totalFrames - startFrame;
    const byteStart = dataOffset + startFrame * bytesPerFrame;
    const byteEnd = byteStart + numFrames * bytesPerFrame;
    const pcm = await file.slice(byteStart, byteEnd).arrayBuffer();

    // Convert the raw PCM slice → Float32 channels → 16-bit WAV.
    const dv = new DataView(pcm);
    const bps = fmt.bitsPerSample;
    const channels = [];
    for (let c = 0; c < fmt.numChannels; c++) channels.push(new Float32Array(numFrames));
    for (let i = 0; i < numFrames; i++) {
        for (let c = 0; c < fmt.numChannels; c++) {
            const o = (i * fmt.numChannels + c) * (bps / 8);
            let v = 0;
            if (bps === 16) v = dv.getInt16(o, true) / 0x8000;
            else if (bps === 8) v = (dv.getUint8(o) - 128) / 128;
            else if (bps === 24) { const b0 = dv.getUint8(o), b1 = dv.getUint8(o+1), b2 = dv.getUint8(o+2); let x = (b2 << 16) | (b1 << 8) | b0; if (x & 0x800000) x -= 0x1000000; v = x / 0x800000; }
            else if (bps === 32) v = dv.getInt32(o, true) / 0x80000000;
            channels[c][i] = v;
        }
    }
    return encodeWav(channels, fmt.sampleRate);
}

// Fallback for FLAC/MP3 (or non-PCM WAV): decode the whole file, then slice.
// Heavier on memory — used only when the byte-slice path can't apply.
let _audioCtx = null;
async function cutByDecode(file, startSec, durSec) {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const buf = await file.arrayBuffer();
    const audio = await _audioCtx.decodeAudioData(buf);
    const sr = audio.sampleRate;
    let startFrame = Math.max(0, Math.floor(startSec * sr));
    if (startFrame >= audio.length) return null;
    let numFrames = Math.floor(durSec * sr);
    if (startFrame + numFrames > audio.length) numFrames = audio.length - startFrame;
    const channels = [];
    for (let c = 0; c < audio.numberOfChannels; c++) {
        channels.push(audio.getChannelData(c).subarray(startFrame, startFrame + numFrames));
    }
    return encodeWav(channels, sr);
}

    // Cut one planned segment out of its recording. `duration` and `shift` are
    // the operator's choices; a negative start is clamped, since a detection at
    // second 0 with a negative shift is still a valid clip.
    async function cut(file, seg, duration, shift) {
        let start = (seg.start_s || 0) + (shift || 0);
        if (start < 0) start = 0;
        const dur = duration || 5;
        if (/\.wav$/i.test(file.name)) {
            const w = await cutWavByBytes(file, start, dur);
            if (w) return w;                 // fast path succeeded
        }
        return await cutByDecode(file, start, dur);   // flac / mp3 / non-PCM wav
    }

    // Cut + upload one segment. Returns the outcome as a word, so the caller
    // owns both the counters and the wording of its log.
    //   'saved' | 'duplicate' | 'nofile' | 'error'
    async function uploadOne(seg, opts) {
        const file = opts.fileMap.get((seg.recording_filename || '').toLowerCase());
        if (!file) return { status: 'nofile' };
        try {
            const blob = await cut(file, seg, opts.duration, opts.shift);
            if (!blob) return { status: 'nofile' };
            const fd = new FormData();
            fd.append('segment', blob,
                      seg.segment_filename.replace(/\.(flac|mp3)$/i, '.wav'));
            fd.append('species_name', opts.speciesName || '');
            fd.append('species_id', seg.species_id);
            fd.append('detection_id', seg.detection_id);
            fd.append('recording_id', seg.recording_id);
            fd.append('segment_filename', seg.segment_filename);
            fd.append('confidence', seg.confidence);
            fd.append('location_name', seg.location_name || '');
            const modelId = (seg.model_id != null) ? seg.model_id : opts.modelId;
            if (modelId != null && modelId !== '') fd.append('model_id', modelId);
            if (seg.recorded_date) fd.append('recorded_date', seg.recorded_date);
            if (seg.recorded_time) fd.append('recorded_time', seg.recorded_time);
            const r = await fetch(opts.uploadUrl, {
                method: 'POST',
                headers: { 'X-CSRFToken': opts.csrf },
                body: fd
            });
            const data = await r.json();
            if (data.success && data.status === 'saved') return { status: 'saved' };
            if (data.success && data.status === 'duplicate') return { status: 'duplicate' };
            return { status: 'error', error: data.error || r.status };
        } catch (e) {
            return { status: 'error', error: e.message };
        }
    }

    // Run a whole plan through a small concurrency pool, mirroring the
    // fast-upload path in camera traps. `onResult(seg, outcome, done, total)`
    // fires after every item; the caller counts and renders.
    async function run(opts) {
        const plan = opts.plan || [];
        const total = plan.length;
        const queue = plan.slice();
        const concurrency = opts.concurrency || 4;
        let done = 0;
        const workers = [];
        for (let i = 0; i < concurrency; i++) {
            workers.push((async () => {
                while (queue.length) {
                    const seg = queue.shift();
                    const outcome = await uploadOne(seg, opts);
                    done++;
                    if (opts.onResult) opts.onResult(seg, outcome, done, total);
                }
            })());
        }
        await Promise.all(workers);
        return done;
    }

    // Index a picked folder by lowercase basename, which is how a plan's
    // `recording_filename` is matched.
    function indexFolder(files) {
        const map = new Map();
        for (const f of files) {
            if (AUDIO_EXT.test(f.name)) map.set(f.name.toLowerCase(), f);
        }
        return map;
    }

    global.PamSegmentCutter = {
        AUDIO_EXT: AUDIO_EXT,
        encodeWav: encodeWav,
        parseWavHeader: parseWavHeader,
        cutWavByBytes: cutWavByBytes,
        cutByDecode: cutByDecode,
        cut: cut,
        uploadOne: uploadOne,
        run: run,
        indexFolder: indexFolder
    };
})(window);
