// SPDX-FileCopyrightText: Copyright (C) 2025 ARDUINO SA <http://www.arduino.cc>
//
// SPDX-License-Identifier: MPL-2.0

const socket = io(`http://${window.location.host}`);

const noDataTimeout = 10000; // 10 seconds
let liveCircleTimeout = null;
let errorContainer;

const LAST_BASENAME_KEY = 'magneticApp.lastRecordingBaseName';
const DEFAULT_BASENAME = 'magnetic_field_log';

// Never render more than this many points per colored segment - beyond
// this, points get bucketed and averaged (see downsample()) so the chart
// stays smooth even after a 100k-point measurement. The full-resolution
// data (in obj.segments) is untouched by this - it's purely a rendering
// concern, and it's what's already being written to the CSV regardless.
const MAX_RENDER_POINTS = 1000;

// How often the chart is allowed to actually redraw, in ms. New points
// still accumulate in the background every time they arrive; this just
// caps how often we re-downsample + repaint, so a fast sample rate doesn't
// hammer the browser.
const RENDER_THROTTLE_MS = 500;

// Whether we're currently between "Start" and "Stop" - drives the point
// color on the live chart (green while recording, orange otherwise).
// Initialized from /record/status on load in case the page was refreshed
// mid-recording, then kept in sync by the Start/Stop/Clear handlers.
let isRecording = false;

function colorForSegment(recording) {
    return recording ? 'green' : 'orange';
}

function fillForSegment(recording) {
    return recording ? 'rgba(43,138,62,0.15)' : 'rgba(255,165,0,0.15)';
}

function datasetFor(data, recording) {
    return {
        data,                          // [{x: epoch_ms, y: value}, ...]
        borderColor: colorForSegment(recording),
        backgroundColor: fillForSegment(recording),
        pointBackgroundColor: colorForSegment(recording),
        fill: 'origin',      // fill area between points and y=0, using the (hidden) connecting line
        showLine: false,     // don't draw the connecting line itself, only points
        pointRadius: 2.5,
        pointHoverRadius: 5,
        borderWidth: 1.5,
        tension: 0,
    };
}

// Returns the subrange of `points` (must be sorted ascending by x - true for
// every segment here, since points are always appended in arrival order)
// whose x falls in [rangeMin, rangeMax]. Uses binary search to find the
// boundaries - O(log n) - instead of scanning every point - O(n). This
// matters a lot here: without it, every render tick (up to ~2x/second)
// would re-scan the ENTIRE session's accumulated history just to find the
// small visible window, getting slower and slower as a long/fast-sampled
// session grows. With it, the cost stays tied to the window size, not the
// total history size, however long the session runs.
function sliceByRange(points, rangeMin, rangeMax) {
    let lo = 0, hi = points.length;
    while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (points[mid].x < rangeMin) lo = mid + 1; else hi = mid;
    }
    const start = lo;
    hi = points.length;
    while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (points[mid].x <= rangeMax) lo = mid + 1; else hi = mid;
    }
    return points.slice(start, lo);
}

// Buckets `points` down to at most `maxPoints` entries, averaging x and y
// within each bucket. Assumes roughly-uniform time spacing (true for a
// fixed sample rate), so equal-COUNT buckets approximate equal-TIME
// buckets closely enough for a display average.
function downsample(points, maxPoints) {
    if (points.length <= maxPoints) return points;
    const bucketSize = points.length / maxPoints;
    const out = [];
    for (let b = 0; b < maxPoints; b++) {
        const start = Math.floor(b * bucketSize);
        const end = Math.max(start + 1, Math.floor((b + 1) * bucketSize));
        const chunk = points.slice(start, end);
        if (chunk.length === 0) continue;
        const avgX = chunk.reduce((s, p) => s + p.x, 0) / chunk.length;
        const avgY = chunk.reduce((s, p) => s + p.y, 0) / chunk.length;
        out.push({ x: avgX, y: avgY });
    }
    return out;
}

const magneticLive = {
    canvasId: 'magnetic-live-chart',
    canvas: null,
    chart: null,
    unit: 'µT',
    showStats: false,    // mean / median toggle
    showSpread: false,   // std dev / MAD toggle
    segments: [],        // full-resolution source of truth: [{recording, data: [{x,y}...]}, ...]
    windowSeconds: 3600,       // "show me the last N seconds" - adjustable via the UI
    followWindow: true,       // true = auto-slide to [latestTs - windowSeconds, latestTs]; false = user has manually zoomed/panned
    // The right edge of the rolling window is anchored to the newest data
    // point's OWN timestamp, not the browser's Date.now() - if the board's
    // clock isn't synced with the browser's (very possible on a freshly
    // flashed, offline embedded Linux board), comparing data timestamps
    // against Date.now() can push everything outside the window even
    // though the data is perfectly real, making the chart look empty.
    latestTs: null,
    _lastRenderTs: 0,
    data: {
        datasets: [datasetFor([], false)]
    }
};

document.addEventListener('DOMContentLoaded', () => {
    magneticLive.canvas = document.getElementById(magneticLive.canvasId);

    const liveCircle = document.getElementById('live-circle');
    if (liveCircle) liveCircle.style.display = 'none';

    errorContainer = document.getElementById('error-container');

    // Popover logic for info buttons
    const magnPopoverText = 'Shows magnetic field readings in µT. Drag to pan, scroll/pinch to zoom (this pauses auto-follow - press Reset zoom to resume). Mean/median and std/MAD are computed over the currently visible range, from the full-resolution data - only the drawn points are averaged down for display.';
    document.querySelectorAll('.info-btn.temp').forEach(img => {
        img.style.position = 'relative';
        const popover = img.nextElementSibling;
        img.addEventListener('mouseenter', () => {
            popover.textContent = magnPopoverText;
            popover.style.display = 'block';
        });
        img.addEventListener('mouseleave', () => {
            popover.style.display = 'none';
        });
    });

    setupToolbar();
    setupWindowControl();
    setupRecordingControls();
    setupSampleRateControl();
    setupGainControl();

    initSocketIO();
    loadLiveHistory();
    refreshSavedFiles();

    // Keeps the window sliding forward in real time even if no new data
    // point arrives for a while - "every second the window should move".
    setInterval(() => renderLiveChart(true), 1000);
});

// =============================================================================
// Recording controls (Start / Stop / Save / Clear) + the "name this
// recording" prompt shown only when Start would create a brand-new file.
// =============================================================================
function setupRecordingControls() {
    const startBtn = document.getElementById('rec-start');
    const saveBtn = document.getElementById('rec-save');
    const stopBtn = document.getElementById('rec-stop');
    const clearBtn = document.getElementById('rec-clear');
    const statusEl = document.getElementById('rec-status');
    if (!startBtn) return;

    // POST helper. `query` is appended as a query string - NOT a JSON body -
    // since that's how this framework binds plain str/int function params.
    async function postRecord(action, query) {
        let url = `http://${window.location.host}/record/${action}`;
        if (query) url += `?${new URLSearchParams(query).toString()}`;
        const res = await fetch(url, { method: 'POST' });
        return res.json();
    }

    // The app is now ALWAYS writing to some file - either an auto-named
    // generic one (orange, unsaved-only) or a named one you started
    // (green, saveable). "isRecording" (the global that drives point
    // color) specifically means "a NAMED recording is active", not
    // "anything is being saved" - that's now true almost always.
    function render(status) {
        if (status.error) {
            statusEl.textContent = status.error;
            statusEl.classList.remove('is-recording');
            return;
        }
        if (status.generic) {
            statusEl.textContent = `Auto-recording → ${status.file}`;
            statusEl.classList.remove('is-recording');
        } else if (status.recording) {
            statusEl.textContent = `Recording → ${status.file}`;
            statusEl.classList.add('is-recording');
        } else {
            // Shouldn't really happen anymore (something is always active),
            // kept as a sane fallback just in case.
            statusEl.textContent = status.file ? `Stopped: ${status.file}` : 'Stopped';
            statusEl.classList.remove('is-recording');
        }
    }

    async function fetchStatus() {
        try {
            const res = await fetch(`http://${window.location.host}/record/status`);
            return await res.json();
        } catch (e) {
            console.log(`Could not fetch recording status: ${e.message}`);
            return {};
        }
    }

    async function refreshStatus() {
        const status = await fetchStatus();
        // Resume the correct point color if the page was reloaded mid-recording.
        isRecording = !!status.recording && !status.generic;
        render(status);
    }

    // Start always begins a brand-new NAMED recording, stopping whatever
    // was active before it (generic or named) - there's no more "resume"
    // case, since something is always recording now.
    startBtn.addEventListener('click', () => openStartNameModal());

    stopBtn.addEventListener('click', async () => {
        const status = await postRecord('stop');
        isRecording = false; // back to auto-recording generically
        render(status);
    });

    clearBtn.addEventListener('click', async () => {
        // Only warn about losing data if a NAMED recording is what's about
        // to be abandoned - clearing an ambient generic auto-log isn't the
        // kind of "you might lose your work" moment this warning is for.
        const status = await fetchStatus();
        const hasNamedActive = !!status.file && !status.generic;
        const savedRecently = status.saved_at && (Date.now() - status.saved_at) < 60000;
        if (hasNamedActive && !savedRecently) {
            const proceed = confirm(
                "You haven't saved in the last minute - clearing now will stop tracking the current recording as \"active\" (the file itself stays on disk, but only saved files are easy to find again, and unsaved ones are auto-removed after 7 days). Continue?"
            );
            if (!proceed) return;
        }

        // Clear abandons whatever's active and goes back to auto-recording
        // generically - same as Stop, same as a true boot.
        const s = await postRecord('clear');
        isRecording = false;
        render(s);
        resetLiveChartView(); // wipe the data currently in view
    });

    saveBtn.addEventListener('click', async () => {
        render(await postRecord('save')); // backend rejects this for generic files - the error shows in the status text
        refreshSavedFiles(); // a file just became "saved" - refresh the menu
    });

    // --- "Start recording" name-prompt modal ---
    const modal = document.getElementById('start-name-modal');
    const nameInput = document.getElementById('start-name-input');
    const confirmBtn = document.getElementById('start-name-confirm');
    const cancelBtn = document.getElementById('start-name-cancel');

    function openStartNameModal() {
        const lastName = localStorage.getItem(LAST_BASENAME_KEY) || DEFAULT_BASENAME;
        nameInput.value = lastName;
        modal.classList.add('open');
        setTimeout(() => { nameInput.focus(); nameInput.select(); }, 0);
    }

    function closeStartNameModal() {
        modal.classList.remove('open');
    }

    async function confirmStart() {
        const baseName = (nameInput.value || '').trim() || DEFAULT_BASENAME;
        localStorage.setItem(LAST_BASENAME_KEY, baseName);
        closeStartNameModal();
        const status = await postRecord('start', { base_name: baseName });
        isRecording = true;
        render(status);
    }

    confirmBtn.addEventListener('click', confirmStart);
    cancelBtn.addEventListener('click', closeStartNameModal);
    nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') confirmStart();
        if (e.key === 'Escape') closeStartNameModal();
    });
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeStartNameModal();
    });

    refreshStatus();
}

// Wipe everything currently plotted on the live chart (used by Clear).
function resetLiveChartView() {
    magneticLive.segments = [];
    magneticLive.followWindow = true;
    magneticLive.latestTs = null;
    if (magneticLive.chart) {
        magneticLive.chart.resetZoom();
    }
    renderLiveChart(true);
}

// =============================================================================
// Saved-files menu (shown under the mean/median stats box).
// =============================================================================
async function refreshSavedFiles() {
    let sessions = [];
    try {
        const res = await fetch(`http://${window.location.host}/list_sessions`);
        sessions = await res.json();
    } catch (e) {
        console.log(`Could not fetch saved sessions: ${e.message}`);
        return;
    }

    const savedTop5 = (sessions || [])
        .filter(s => s.saved)
        .sort((a, b) => (b.created || 0) - (a.created || 0))
        .slice(0, 5);

    document.querySelectorAll('.saved-files-list').forEach(listEl => {
        listEl.innerHTML = '';
        if (savedTop5.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'saved-files-empty';
            empty.textContent = 'No saved files yet';
            listEl.appendChild(empty);
            return;
        }
        savedTop5.forEach(s => {
            const a = document.createElement('a');
            a.href = `http://${window.location.host}/download_csv?file=${encodeURIComponent(s.file)}`;
            a.download = s.file;
            a.title = s.file;
            a.textContent = s.file;
            listEl.appendChild(a);
        });
    });

    document.querySelectorAll('.saved-files-viewall').forEach(a => {
        a.href = `http://${window.location.host}/files`;
    });
}

function setupSampleRateControl() {
    const input = document.getElementById('sample-rate-input');
    const setBtn = document.getElementById('sample-rate-set');
    if (!input || !setBtn) return;

    fetch(`http://${window.location.host}/sample_rate`)
        .then(r => r.json())
        .then(data => {
            if (data.interval_ms) input.value = data.interval_ms;
        })
        .catch(e => console.log(`Could not fetch sample rate: ${e.message}`));

    setBtn.addEventListener('click', async () => {
        const ms = parseInt(input.value, 10);
        if (!ms || ms < 50) {
            alert('Enter an interval of at least 50 ms.');
            return;
        }
        try {
            const res = await fetch(`http://${window.location.host}/sample_rate/${ms}`, { method: 'POST' });
            const data = await res.json();
            if (data.error) alert(data.error);
        } catch (e) {
            console.log(`Could not set sample rate: ${e.message}`);
        }
    });
}

// Gain (ADS1115 PGA) control: pick a value from the dropdown + Set for
// manual gain, or toggle Auto to let the backend re-range every minute
// based on the peak voltage it's been seeing. Polls /gain periodically so
// the dropdown reflects whatever auto-gain picks on its own, not just
// changes made from this browser tab.
function setupGainControl() {
    const select = document.getElementById('gain-input');
    const setBtn = document.getElementById('gain-set');
    const autoBtn = document.getElementById('gain-auto-toggle');
    const statusEl = document.getElementById('gain-status');
    if (!select || !setBtn || !autoBtn) return;

    async function refreshGainStatus() {
        try {
            const res = await fetch(`http://${window.location.host}/gain`);
            const data = await res.json();
            if (data.error) {
                if (statusEl) statusEl.textContent = data.error;
                return;
            }
            if (data.gain !== undefined && document.activeElement !== select) {
                select.value = String(data.gain);
            }
            const isAuto = !!data.auto;
            autoBtn.textContent = isAuto ? 'Auto: On' : 'Auto: Off';
            autoBtn.classList.toggle('active', isAuto);
            if (statusEl) statusEl.textContent = isAuto ? '(re-checks every 60s)' : '';
        } catch (e) {
            console.log(`Could not fetch gain status: ${e.message}`);
        }
    }

    setBtn.addEventListener('click', async () => {
        const gain = parseInt(select.value, 10);
        try {
            const res = await fetch(`http://${window.location.host}/gain/${gain}`, { method: 'POST' });
            const data = await res.json();
            if (data.error) alert(data.error);
        } catch (e) {
            console.log(`Could not set gain: ${e.message}`);
        }
        refreshGainStatus();
    });

    autoBtn.addEventListener('click', async () => {
        const turningOn = !autoBtn.classList.contains('active');
        try {
            await fetch(`http://${window.location.host}/gain_auto/${turningOn}`, { method: 'POST' });
        } catch (e) {
            console.log(`Could not toggle auto-gain: ${e.message}`);
        }
        refreshGainStatus();
    });

    refreshGainStatus();
    setInterval(refreshGainStatus, 5000); // pick up auto-gain's own periodic changes
}

// "Show last ___ seconds" control.
function setupWindowControl() {
    const input = document.getElementById('window-seconds-input');
    const setBtn = document.getElementById('window-seconds-set');
    if (!input || !setBtn) return;

    input.value = magneticLive.windowSeconds;

    function apply() {
        const secs = parseInt(input.value, 10);
        if (!secs || secs < 1) {
            alert('Enter a positive number of seconds.');
            return;
        }
        magneticLive.windowSeconds = secs;
        magneticLive.followWindow = true; // picking a window size implies "follow live" again
        if (magneticLive.chart) magneticLive.chart.resetZoom();
        renderLiveChart(true);
    }

    setBtn.addEventListener('click', apply);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') apply(); });
}

async function loadLiveHistory() {
    let samples = [];
    try {
        const res = await fetch(`http://${window.location.host}/live_history`);
        samples = await res.json();
    } catch (e) {
        console.log(`Could not fetch live history: ${e.message}`);
    }
    const points = (samples || [])
        .filter(m => m && m.ts !== undefined && m.ts !== null)
        .map(m => ({ x: m.ts, y: m.value }))
        .sort((a, b) => a.x - b.x);
    magneticLive.segments = points.length ? [{ recording: false, data: points }] : [];
    if (points.length) magneticLive.latestTs = points[points.length - 1].x;
    renderLiveChart(true);
}

function setupToolbar() {
    const obj = magneticLive;

    const resetBtn = document.getElementById(obj.canvasId + '-reset-zoom');
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            if (obj.chart) obj.chart.resetZoom();
            obj.followWindow = true; // resume auto-follow
            renderLiveChart(true);
        });
    }

    const statsToggle = document.getElementById(obj.canvasId + '-stats-toggle');
    if (statsToggle) {
        statsToggle.checked = obj.showStats;
        statsToggle.addEventListener('change', () => {
            obj.showStats = statsToggle.checked;
            updateStats(obj);
        });
    }

    const spreadToggle = document.getElementById(obj.canvasId + '-spread-toggle');
    if (spreadToggle) {
        spreadToggle.checked = obj.showSpread;
        spreadToggle.addEventListener('change', () => {
            obj.showSpread = spreadToggle.checked;
            updateStats(obj);
        });
    }
}

function initSocketIO() {
    socket.on('connect', () => {
        if (errorContainer) {
            errorContainer.style.display = 'none';
            errorContainer.textContent = '';
        }
    });

    socket.on('disconnect', () => {
        if (errorContainer) {
            errorContainer.textContent = 'Connection to the board lost. Please check the connection.';
            errorContainer.style.display = 'block';
        }
    });

    socket.on('B_field', (message) => {
        appendLivePoint(message);
    });
}

// Append a single point (used for live socket updates) to the full-res
// source of truth, then ask for a (throttled) repaint.
function appendLivePoint(message) {
    if (!message || message.ts === undefined || message.ts === null) return;
    const point = { x: message.ts, y: message.value };

    const segments = magneticLive.segments;
    const lastSeg = segments[segments.length - 1];
    if (!lastSeg || lastSeg.recording !== isRecording) {
        // Recording state just flipped (or this is the first point) - start a new colored segment.
        segments.push({ recording: isRecording, data: [point] });
    } else {
        lastSeg.data.push(point);
    }
    magneticLive.latestTs = point.x;

    renderLiveChart(false); // throttled - doesn't necessarily repaint immediately
}

// The single entry point for (re)painting the live chart: figures out the
// current visible range (the rolling window, or wherever the user manually
// zoomed to), slices+downsamples each segment to that range, and updates
// the chart + stats. Called on new data, on the 1s heartbeat, and whenever
// the window size or zoom state changes.
function renderLiveChart(force) {
    const obj = magneticLive;
    const now = Date.now();
    if (!force && obj._lastRenderTs && now - obj._lastRenderTs < RENDER_THROTTLE_MS) return;
    obj._lastRenderTs = now;

    const noDataDiv = document.getElementById(obj.canvasId + '-nodata');
    const liveCircle = document.getElementById('live-circle');
    const hasAnyData = obj.segments.some(seg => seg.data.length > 0);

    if (!hasAnyData) {
        if (obj.canvas) obj.canvas.style.display = 'none';
        if (noDataDiv) noDataDiv.style.display = 'flex';
        if (liveCircle) {
            liveCircle.style.display = 'none';
            liveCircle.classList.remove('flash');
            if (liveCircleTimeout) {
                clearTimeout(liveCircleTimeout);
                liveCircleTimeout = null;
            }
        }
        return;
    }

    if (obj.canvas) obj.canvas.style.display = 'block';
    if (noDataDiv) noDataDiv.style.display = 'none';
    if (liveCircle) {
        liveCircle.style.display = 'flex';
        liveCircle.classList.add('flash');
        if (liveCircleTimeout) clearTimeout(liveCircleTimeout);
        liveCircleTimeout = setTimeout(() => {
            liveCircle.classList.remove('flash');
            liveCircle.style.display = 'none';
        }, noDataTimeout);
    }

    // Decide the visible range: auto-follow the rolling window, unless the
    // user has manually zoomed/panned (in which case leave their view alone).
    // Anchored to the data's own latest timestamp (obj.latestTs), not the
    // browser's clock - see the comment on obj.latestTs above for why.
    let rangeMin, rangeMax;
    if (obj.followWindow || !obj.chart) {
        rangeMax = obj.latestTs !== null ? obj.latestTs : now;
        rangeMin = rangeMax - obj.windowSeconds * 1000;
    } else {
        rangeMin = obj.chart.scales.x.min;
        rangeMax = obj.chart.scales.x.max;
    }

    // Slice each segment to the visible range, then downsample - the total
    // rendered points across all segments is capped at MAX_RENDER_POINTS,
    // split proportionally by how many raw points each segment contributes.
    const inRangeSegs = obj.segments.map(seg => sliceByRange(seg.data, rangeMin, rangeMax));
    const totalInRange = inRangeSegs.reduce((s, a) => s + a.length, 0);

    obj.data.datasets = obj.segments.map((seg, i) => {
        const pts = inRangeSegs[i];
        const share = totalInRange > 0
            ? Math.max(1, Math.round(MAX_RENDER_POINTS * pts.length / totalInRange))
            : MAX_RENDER_POINTS;
        return datasetFor(downsample(pts, Math.min(share, MAX_RENDER_POINTS)), seg.recording);
    });

    if (!obj.chart) {
        obj.chart = newChart(obj);
    }

    if (obj.followWindow) {
        obj.chart.options.scales.x.min = rangeMin;
        obj.chart.options.scales.x.max = rangeMax;
    }
    obj.chart.update('none');

    updateStats(obj, rangeMin, rangeMax, inRangeSegs);
}

function newChart(obj) {
    const ctx = obj.canvas.getContext('2d');
    return new Chart(ctx, {
        type: 'line',
        data: obj.data,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            parsing: false,
            scales: {
                y: {
                    title: {
                        display: true,
                        text: `Magnetic field (${obj.unit})`
                    }
                },
                x: {
                    type: 'time',
                    time: {
                        tooltipFormat: 'PP HH:mm:ss'
                    },
                    grid: { display: false },
                    ticks: {
                        maxRotation: 45,
                        minRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: 10
                    }
                }
            },
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString() : '',
                        label: (context) => `${context.parsed.y.toFixed(2)} ${obj.unit}`
                    }
                },
                zoom: {
                    pan: {
                        enabled: true,
                        mode: 'x',
                        onPanComplete: () => {
                            obj.followWindow = false; // manual pan - stop auto-following until Reset zoom
                            renderLiveChart(true);
                        }
                    },
                    zoom: {
                        wheel: { enabled: true },
                        pinch: { enabled: true },
                        drag: { enabled: false },
                        mode: 'x',
                        onZoomComplete: () => {
                            obj.followWindow = false;
                            renderLiveChart(true);
                        }
                    },
                    limits: {
                        x: { minRange: 5000 } // don't allow zooming past ~5s window
                    }
                },
                annotation: {
                    annotations: {
                        // Bands drawn first so the mean/median lines render on top of them.
                        stdBand: {
                            type: 'box',
                            yMin: null,
                            yMax: null,
                            backgroundColor: 'rgba(112,72,232,0.10)',
                            borderWidth: 0,
                            display: false
                        },
                        madBand: {
                            type: 'box',
                            yMin: null,
                            yMax: null,
                            backgroundColor: 'rgba(12,166,120,0.10)',
                            borderWidth: 0,
                            display: false
                        },
                        meanLine: {
                            type: 'line',
                            yMin: null,
                            yMax: null,
                            borderColor: '#2b8a3e',
                            borderWidth: 2,
                            borderDash: [6, 4],
                            display: false
                        },
                        medianLine: {
                            type: 'line',
                            yMin: null,
                            yMax: null,
                            borderColor: '#1864ab',
                            borderWidth: 2,
                            borderDash: [2, 3],
                            display: false
                        }
                    }
                }
            }
        }
    });
}

// Recompute mean/median/std/MAD over the currently visible x-range, always
// from the full-resolution data in obj.segments (never the downsampled
// render points), and update both the on-chart lines/bands and the side
// stats panel. rangeMin/rangeMax are optional - if omitted, falls back to
// whatever the chart's x-scale currently shows. inRangeSegs is also
// optional - renderLiveChart already computes it (via binary search) and
// passes it straight in so it isn't sliced twice; callers without one on
// hand (the toggle checkboxes, Reset zoom) fall back to slicing it here.
function updateStats(obj, rangeMin, rangeMax, inRangeSegs) {
    if (!obj.chart) return;
    if (rangeMin === undefined) rangeMin = obj.chart.scales.x.min;
    if (rangeMax === undefined) rangeMax = obj.chart.scales.x.max;

    const statsPanel = document.getElementById(obj.canvasId + '-stats');
    const annotations = obj.chart.options.plugins.annotation.annotations;

    if (!inRangeSegs) {
        inRangeSegs = obj.segments.map(seg => sliceByRange(seg.data, rangeMin, rangeMax));
    }
    const visiblePoints = inRangeSegs.reduce((acc, arr) => acc.concat(arr), []);

    const anyStatsOn = obj.showStats || obj.showSpread;

    if (!anyStatsOn || visiblePoints.length === 0) {
        annotations.meanLine.display = false;
        annotations.medianLine.display = false;
        annotations.stdBand.display = false;
        annotations.madBand.display = false;
        if (statsPanel) {
            statsPanel.innerHTML = visiblePoints.length === 0
                ? '<div class="stat-row stat-count">No data in view</div>'
                : '<div class="stat-row stat-count">Stats hidden</div>';
        }
        obj.chart.update('none');
        return;
    }

    const values = visiblePoints.map(p => p.y).sort((a, b) => a - b);
    const n = values.length;
    const mean = values.reduce((s, v) => s + v, 0) / n;
    const mid = Math.floor(n / 2);
    const median = n % 2 !== 0
        ? values[mid]
        : (values[mid - 1] + values[mid]) / 2;

    // Sample standard deviation (n-1 denominator).
    let std = 0;
    if (n > 1) {
        const variance = values.reduce((s, v) => s + (v - mean) * (v - mean), 0) / (n - 1);
        std = Math.sqrt(variance);
    }

    // Median absolute deviation: median of |x - median|. More robust to
    // outliers than std, since a single wild spike can't drag it far.
    const absDevs = values.map(v => Math.abs(v - median)).sort((a, b) => a - b);
    const amid = Math.floor(n / 2);
    const mad = n % 2 !== 0
        ? absDevs[amid]
        : (absDevs[amid - 1] + absDevs[amid]) / 2;

    annotations.meanLine.display = obj.showStats;
    annotations.meanLine.yMin = mean;
    annotations.meanLine.yMax = mean;

    annotations.medianLine.display = obj.showStats;
    annotations.medianLine.yMin = median;
    annotations.medianLine.yMax = median;

    annotations.stdBand.display = obj.showSpread;
    annotations.stdBand.yMin = mean - std;
    annotations.stdBand.yMax = mean + std;

    annotations.madBand.display = obj.showSpread;
    annotations.madBand.yMin = median - mad;
    annotations.madBand.yMax = median + mad;

    let html = '';
    if (obj.showStats || obj.showSpread) {
        html += '<div class="stat-pair">';
        if (obj.showStats) html += `<div class="stat-cell"><span class="stat-swatch mean"></span>Mean<br><b>${mean.toFixed(2)} ${obj.unit}</b></div>`;
        if (obj.showSpread) html += `<div class="stat-cell"><span class="stat-swatch std"></span>Std ±<br><b>${std.toFixed(4)} ${obj.unit}</b></div>`;
        html += '</div>';
        html += '<div class="stat-pair">';
        if (obj.showStats) html += `<div class="stat-cell"><span class="stat-swatch median"></span>Median<br><b>${median.toFixed(2)} ${obj.unit}</b></div>`;
        if (obj.showSpread) html += `<div class="stat-cell"><span class="stat-swatch mad"></span>MAD ±<br><b>${mad.toFixed(4)} ${obj.unit}</b></div>`;
        html += '</div>';
    }
    html += `<div class="stat-row stat-count">${n} raw pts in view</div>`;

    if (statsPanel) statsPanel.innerHTML = html;

    obj.chart.update('none');
}
