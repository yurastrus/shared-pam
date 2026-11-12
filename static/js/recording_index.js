// SPDX-License-Identifier: AGPL-3.0-only
(function (global) {
    'use strict';
    const MARKER = 'location.json';
    const CATALOG = '.pam-location-folders-v1.json';
    const VERSION = 1;

    function absent(error) {
        return error.name === 'NotFoundError' || error.name === 'TypeMismatchError';
    }

    function rejectedName(error) {
        return error.name === 'TypeError' && error.message.includes('Name is not allowed');
    }

    async function readMetadata(dir, name, valid) {
        try {
            const handle = await dir.getFileHandle(name);
            const data = JSON.parse(await (await handle.getFile()).text());
            return data?.pam_version === VERSION && valid(data) ? data : null;
        } catch (error) {
            if (absent(error) || error instanceof SyntaxError) return null;
            throw error;
        }
    }

    async function writeMetadata(dir, name, data) {
        const handle = await dir.getFileHandle(name, { create: true });
        const writer = await handle.createWritable();
        try {
            await writer.write(JSON.stringify({ pam_version: VERSION, ...data }));
            await writer.close();
        } catch (error) {
            await writer.abort().catch(() => {});
            throw error;
        }
    }

    function validLocations(locations) {
        return Array.isArray(locations) && locations.every(location =>
            Number.isInteger(location?.location_id));
    }

    function validCatalog(data) {
        return Array.isArray(data.folders) && data.folders.every(folder =>
            Array.isArray(folder?.path) && folder.path.every(part =>
                typeof part === 'string' && part && part !== '.' && part !== '..' && !/[\\/]/.test(part)
            ) && validLocations(folder.locations));
    }

    function key(segment) {
        return segment.location_id + ':' + basename(segment.recording_filename).toLowerCase();
    }

    function basename(filename) {
        return filename.split(/[\\/]/).at(-1);
    }

    async function rebuild(root, lookup, missing, onProgress) {
        const folders = [];
        let count = 0;
        let lastUpdate = performance.now();

        async function updateLocations(dir, saved, names, leaf) {
            let locations = saved?.locations || [];
            if (names.length) {
                const resolved = await lookup(names);
                // Different managers may only resolve their own locations.
                const byId = new Map(locations.map(location => [location.location_id, location]));
                for (const location of Object.values(resolved)) byId.set(location.location_id, location);
                locations = [...byId.values()].sort((a, b) => a.location_id - b.location_id);
            }
            if (!saved || saved.leaf !== leaf || JSON.stringify(locations) !== JSON.stringify(saved.locations)) {
                await writeMetadata(dir, MARKER, { locations, leaf });
            }
            return locations;
        }

        async function walk(dir, path) {
            const saved = await readMetadata(dir, MARKER, data => validLocations(data.locations));
            if (saved?.leaf === true) {
                const known = new Set(saved.locations.map(location => location.location_id));
                const names = [];
                const wanted = Array.from(missing?.values() || []).filter(recording =>
                    recording.locationIds.some(id => !known.has(id)));
                for (let i = 0; i < wanted.length; i += 16) {
                    await Promise.all(wanted.slice(i, i + 16).map(async ({ filename }) => {
                        try {
                            const handle = await dir.getFileHandle(filename);
                            names.push(handle.name);
                        } catch (error) {
                            if (!absent(error) && !rejectedName(error)) throw error;
                        }
                    }));
                }
                const locations = await updateLocations(dir, saved, names, true);
                folders.push({ path, locations });
                return;
            }
            const names = [];
            const children = [];
            let hasAudio = false;
            for await (const entry of dir.values()) {
                if (entry.kind === 'directory') {
                    children.push(entry);
                } else if (global.PamSegmentCutter.AUDIO_EXT.test(entry.name)) {
                    hasAudio = true;
                    const wanted = missing?.get(entry.name.toLowerCase());
                    if (!saved || wanted?.locationIds.some(id => !saved.locations.some(loc => loc.location_id === id))) {
                        names.push(entry.name);
                    }
                    count++;
                }
                if (performance.now() - lastUpdate >= 100) {
                    onProgress(count);
                    await new Promise(resolve => setTimeout(resolve, 0));
                    lastUpdate = performance.now();
                }
            }
            if (hasAudio) {
                const locations = await updateLocations(dir, saved, names, children.length === 0);
                folders.push({ path, locations });
            }
            for (const child of children) await walk(child, [...path, child.name]);
        }

        onProgress(0);
        await walk(root, []);
        onProgress(count);
        await writeMetadata(root, CATALOG, { folders });
        return folders;
    }

    async function match(root, folders, plan) {
        const byLocation = new Map();
        for (const folder of folders) {
            for (const { location_id } of folder.locations) {
                if (!byLocation.has(location_id)) byLocation.set(location_id, []);
                byLocation.get(location_id).push(folder.path);
            }
        }
        const directories = new Map([['', Promise.resolve(root)]]);
        function directory(path) {
            const name = path.join('/');
            if (!directories.has(name)) {
                directories.set(name, directory(path.slice(0, -1)).then(dir => dir.getDirectoryHandle(path.at(-1))));
            }
            return directories.get(name);
        }

        const files = new Map();
        const missing = new Map();
        let missingCount = 0;
        let ambiguous = 0;
        const wanted = [...new Map(plan.filter(seg => seg.recording_filename).map(seg => [key(seg), seg]))];
        for (let i = 0; i < wanted.length; i += 16) {
            await Promise.all(wanted.slice(i, i + 16).map(async ([id, segment]) => {
                let found = null;
                const filename = basename(segment.recording_filename);
                for (const path of byLocation.get(segment.location_id) || []) {
                    try {
                        const dir = await directory(path);
                        const handle = await dir.getFileHandle(filename);
                        const file = await handle.getFile();
                        if (found) {
                            ambiguous++;
                            return;
                        }
                        found = file;
                    } catch (error) {
                        if (rejectedName(error)) {
                            missingCount++;
                            return;
                        }
                        if (!absent(error)) throw error;
                    }
                }
                if (found) {
                    files.set(id, found);
                } else {
                    const name = filename.toLowerCase();
                    if (!missing.has(name)) missing.set(name, { filename, locationIds: [] });
                    missing.get(name).locationIds.push(segment.location_id);
                    missingCount++;
                }
            }));
        }
        return { files, missing, missingCount, ambiguous };
    }

    async function find(root, plan, lookup, onProgress, searchArchive) {
        const saved = await readMetadata(root, CATALOG, validCatalog);
        const cached = saved ? await match(root, saved.folders, plan) : null;
        if (cached && (!searchArchive || !cached.missing.size)) return cached;
        const folders = await rebuild(root, lookup, cached?.missing, onProgress);
        return match(root, folders, plan);
    }

    global.PamRecordingIndex = {
        supported: typeof global.showDirectoryPicker === 'function',
        key,
        find(root, plan, lookup, onProgress, { searchArchive = false } = {}) {
            const run = () => find(root, plan, lookup, onProgress, searchArchive);
            return navigator.locks
                ? navigator.locks.request('pam-location-folders', run)
                : run();
        }
    };
})(window);
