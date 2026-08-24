// Client-side donation-zip slimming, shared by the admin ingestion modal
// (data_management.js) and the participant upload on My Collections
// (my_collections.js). Rebuilds a donation zip with only the members the
// server ingester needs — this is what keeps a multi-GB platform export
// under Cloud Run's 32 MiB request cap.
(function () {
    'use strict';

    let _zipLibPromise = null;

    function loadZipLib() {
        // Lazy one-time injection of the vendored zip.js UMD build — nothing is
        // loaded for platforms whose uploads are consumed whole (e.g. TikTok).
        if (window.zip) return Promise.resolve(window.zip);
        if (!_zipLibPromise) {
            _zipLibPromise = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = '/static/js/vendor/zip-full.min.js';
                script.onload = () => {
                    window.zip.configure({ useWebWorkers: false });
                    resolve(window.zip);
                };
                script.onerror = () => {
                    _zipLibPromise = null;
                    reject(new Error('zip.js failed to load'));
                };
                document.head.appendChild(script);
            });
        }
        return _zipLibPromise;
    }

    function formatBytes(bytes) {
        if (!(bytes >= 0)) return '?';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
        return `${bytes >= 100 || i === 0 ? Math.round(bytes) : bytes.toFixed(1)} ${units[i]}`;
    }

    async function repackDonationZip(file, suffixes, onStatus) {
        // Rebuild the donation zip with only the members the server ingester
        // needs, mirroring fyp.utils.read_zip_members semantics: path-suffix
        // matching, directory entries skipped, first match per suffix wins.
        // Member paths and the upload filename are preserved so the server-side
        // ingest path is untouched.
        let zipLib;
        try {
            zipLib = await loadZipLib();
        } catch (err) {
            console.warn('zip.js unavailable, uploading original file:', err);
            return { file, action: 'passthrough' };
        }
        let reader = null;
        try {
            reader = new zipLib.ZipReader(new zipLib.BlobReader(file));
            const entries = await reader.getEntries();
            const remaining = new Set(suffixes);
            const matches = [];
            for (const entry of entries) {
                if (entry.directory || remaining.size === 0) continue;
                for (const suffix of remaining) {
                    if (entry.filename.endsWith(suffix)) {
                        matches.push(entry);
                        remaining.delete(suffix);
                        break;
                    }
                }
            }
            if (matches.length === 0) return { file, action: 'blocked' };

            const writer = new zipLib.ZipWriter(new zipLib.BlobWriter('application/zip'));
            for (const entry of matches) {
                onStatus(`Extracting ${entry.filename}...`);
                const blob = await entry.getData(new zipLib.BlobWriter());
                await writer.add(entry.filename, new zipLib.BlobReader(blob));
            }
            const outBlob = await writer.close();
            const outFile = new File([outBlob], file.name,
                { type: 'application/zip', lastModified: file.lastModified });
            return { file: outFile, action: 'repacked', originalSize: file.size, newSize: outFile.size };
        } catch (err) {
            console.warn(`zip repack failed for ${file.name}, uploading original:`, err);
            return { file, action: 'passthrough' };
        } finally {
            if (reader) { try { await reader.close(); } catch (err) { /* already closed */ } }
        }
    }

    window.DonationZip = { loadZipLib, repackDonationZip, formatBytes };
})();
