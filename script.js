document.addEventListener('DOMContentLoaded', () => {

    const ZONES = ['home', 'kitchen', 'hall'];
    const ZONE_COUNT = ZONES.length;

    const rs = {
        home: { motion: false, light: false, timer: null },
        kitchen: { motion: false, light: false, timer: null },
        hall: { motion: false, light: false, timer: null }
    };

    const POLL_MS = 1000;

    const themeBtn = document.getElementById('theme-toggle');
    const logList = document.querySelector('.log-list');
    const lightsOnCountEl = document.getElementById('lights-on-count');
    const esp32LastSeenEl = document.getElementById('esp32-last-seen');
    const esp32RawStateEl = document.getElementById('esp32-raw-state');
    const dataSourceEl = document.getElementById('data-source');

    let lastLightsOn = null;
    let lastLightsPattern = null;
    let lastAlertUtc = null;
    let lastEsp32Seen = null;
    let lastHttpState = null;

    function formatTime(iso) {
        if (!iso) return '—';
        try {
            const d = new Date(iso);
            return d.toLocaleString();
        } catch {
            return iso;
        }
    }

    themeBtn.addEventListener('click', () => {
        document.body.classList.toggle('dark-mode');
        const icon = themeBtn.querySelector('i');
        if (document.body.classList.contains('dark-mode')) {
            icon.classList.remove('fa-moon');
            icon.classList.add('fa-sun');
            addLog('System', 'Switched to Dark Mode', 'system');
        } else {
            icon.classList.remove('fa-sun');
            icon.classList.add('fa-moon');
            addLog('System', 'Switched to Light Mode', 'system');
        }
    });

    function addLog(roomName, message, type = 'system') {
        if (!logList) return;
        const time = new Date().toLocaleTimeString('en-US', { hour12: false });
        const entry = document.createElement('div');
        entry.className = 'log-item';
        entry.innerHTML = `<span class="log-time">[${time}]</span> <span class="log-event ${type}">${roomName}: ${message}</span>`;

        if (logList.firstChild) {
            logList.insertBefore(entry, logList.firstChild);
        } else {
            logList.appendChild(entry);
        }

        if (logList.children.length > 30) {
            logList.removeChild(logList.lastChild);
        }
    }

    function updateRoomUI(roomId) {
        const card = document.getElementById(`room-${roomId}`);
        const state = rs[roomId];
        if (!card) return;

        const motionValue = card.querySelector('.motion-status .value');
        if (state.motion) {
            motionValue.textContent = 'Yes';
            motionValue.setAttribute('data-status', 'detected');
        } else {
            motionValue.textContent = 'No';
            motionValue.setAttribute('data-status', 'not-detected');
        }

        const lightIndicator = card.querySelector('.light-status .indicator');
        const lightText = card.querySelector('.light-status .status-text');
        const bulbWrapper = card.querySelector('.bulb-wrapper');

        if (state.light) {
            lightIndicator.classList.remove('off');
            lightIndicator.classList.add('on');
            lightText.textContent = 'ON';
            card.classList.add('light-active');
            if (bulbWrapper) {
                bulbWrapper.classList.remove('off');
                bulbWrapper.classList.add('on');
            }
        } else {
            lightIndicator.classList.remove('on');
            lightIndicator.classList.add('off');
            lightText.textContent = 'OFF';
            card.classList.remove('light-active');
            if (bulbWrapper) {
                bulbWrapper.classList.remove('on');
                bulbWrapper.classList.add('off');
            }
        }
    }

    function applyLightsPattern(pattern) {
        let onCount = 0;
        ZONES.forEach((roomId, idx) => {
            const on = Array.isArray(pattern) ? Boolean(pattern[idx]) : false;
            rs[roomId].light = on;
            rs[roomId].motion = on;
            if (on) onCount += 1;
            updateRoomUI(roomId);
        });
        if (lightsOnCountEl) lightsOnCountEl.textContent = `${onCount} / ${ZONE_COUNT}`;
    }

    async function api(path, options) {
        const res = await fetch(path, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });
        return await res.json();
    }

    async function refreshState() {
        try {
            const data = await api('/api/state');
            if (!data || !data.ok) return;
            const st = data.state;

            if (st.esp32_last_seen_utc !== lastEsp32Seen) {
                lastEsp32Seen = st.esp32_last_seen_utc;
                if (esp32LastSeenEl) {
                    esp32LastSeenEl.textContent = formatTime(st.esp32_last_seen_utc);
                    if (st.esp32_last_seen_utc) {
                        esp32LastSeenEl.classList.remove('off');
                        esp32LastSeenEl.classList.add('on');
                    } else {
                        esp32LastSeenEl.classList.remove('on');
                        esp32LastSeenEl.classList.add('off');
                    }
                }
            }

            if (st.last_http_state !== lastHttpState) {
                lastHttpState = st.last_http_state;
                if (esp32RawStateEl) esp32RawStateEl.textContent = st.last_http_state != null ? String(st.last_http_state) : '—';
            }

            if (dataSourceEl) {
                dataSourceEl.textContent = st.last_source === 'http' ? 'ESP32 (HTTP)' : 'Waiting for ESP32…';
            }

            if (Array.isArray(st.lights) && st.lights.length >= ZONE_COUNT) {
                const normalized = st.lights.slice(0, ZONE_COUNT).map(Boolean);
                const patternKey = normalized.map(v => (v ? '1' : '0')).join('');
                if (patternKey !== lastLightsPattern) {
                    lastLightsPattern = patternKey;
                    const prev = lastLightsOn;
                    const current = normalized.filter(Boolean).length;
                    lastLightsOn = current;
                    applyLightsPattern(normalized);
                    if (prev !== null) {
                        addLog('System', `Zones ON: ${current}/${ZONE_COUNT} (state=${st.last_http_state ?? '—'})`, current > prev ? 'on' : 'off');
                    }
                }
            }

            if (Array.isArray(st.alerts) && st.alerts.length) {
                const newest = st.alerts[st.alerts.length - 1];
                if (newest && newest.utc && newest.utc !== lastAlertUtc) {
                    lastAlertUtc = newest.utc;
                    const type = newest.level === 'error' ? 'off' : (newest.level === 'warning' ? 'on' : 'system');
                    addLog('Alert', `${newest.title} - ${newest.message}`, type);
                    window.alert(`${newest.title}\n\n${newest.message}`);
                }
            }
        } catch (e) {
            // keep polling
        }
    }

    addLog('System', 'Dashboard initialized (HTTP only)', 'system');
    applyLightsPattern([false, false, false]);
    refreshState();
    setInterval(refreshState, POLL_MS);

    document.querySelectorAll('.override-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const target = e.target.getAttribute('data-target');
            if (!target || !rs[target]) return;
            const state = rs[target];
            const names = { home: 'Home', kitchen: 'Kitchen', hall: 'Hall' };
            const roomName = names[target] || target;

            state.light = !state.light;
            state.motion = state.light;

            if (state.light) {
                addLog(roomName, 'Manual override: ON', 'on');
            } else {
                addLog(roomName, 'Manual override: OFF', 'off');
            }

            updateRoomUI(target);
            if (lightsOnCountEl) {
                const n = ZONES.filter(z => rs[z].light).length;
                lightsOnCountEl.textContent = `${n} / ${ZONE_COUNT}`;
            }
        });
    });
});
