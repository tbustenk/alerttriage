/* AlertTriage Dashboard – shared utilities */

// Global Chart.js defaults for dark theme
if (typeof Chart !== 'undefined') {
    Chart.defaults.color = '#94a3b8';
    Chart.defaults.borderColor = '#334155';
    Chart.defaults.font.family = "'Segoe UI', system-ui, -apple-system, sans-serif";
    Chart.defaults.font.size = 12;
}

function updateTimestamp() {
    const el = document.getElementById('last-updated');
    if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
    const dot = document.getElementById('refresh-dot');
    if (dot) {
        dot.style.color = '#22c55e';
        setTimeout(() => { dot.style.color = '#64748b'; }, 2000);
    }
}

function showAlert(msg, type = 'success', containerId = 'alert-container') {
    const c = document.getElementById(containerId);
    if (!c) return;
    const el = document.createElement('div');
    el.className = `alert alert-${type}`;
    el.textContent = msg;
    c.innerHTML = '';
    c.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

function fmtDate(ts) {
    if (!ts) return '—';
    try {
        const s = ts.includes('T') ? ts : ts.replace(' ', 'T');
        return new Date(s.endsWith('Z') ? s : s + 'Z').toLocaleString();
    } catch {
        return ts;
    }
}

function fmtDateShort(ts) {
    if (!ts) return '—';
    try {
        const s = ts.includes('T') ? ts : ts.replace(' ', 'T');
        return new Date(s.endsWith('Z') ? s : s + 'Z').toLocaleDateString();
    } catch {
        return ts;
    }
}

function verdictBadge(v) {
    const map = {
        false_positive: ['badge-fp',        'False Positive'],
        true_positive:  ['badge-tp',        'True Positive'],
        escalated:      ['badge-escalated', 'Escalated'],
        closed:         ['badge-closed',    'Closed'],
    };
    const [cls, label] = map[v] || ['badge-closed', v || '—'];
    return `<span class="badge ${cls}">${label}</span>`;
}

function correctBadge(correct) {
    return correct
        ? '<span class="badge badge-correct">&#10003; Correct</span>'
        : '<span class="badge badge-incorrect">&#10007; Wrong</span>';
}

function loadingHTML() {
    return '<div class="loading"><div class="spinner"></div>Loading...</div>';
}

function emptyHTML(icon = '📭', msg = 'No data yet.') {
    return `<div class="empty-state"><div class="empty-icon">${icon}</div><p>${msg}</p></div>`;
}

// Auto-refresh every 5 minutes
function setupAutoRefresh(fn) {
    setInterval(() => { fn(); updateTimestamp(); }, 5 * 60 * 1000);
}

// Mobile nav toggle
document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('nav-toggle');
    const links  = document.querySelector('.nav-links');
    if (toggle && links) {
        toggle.addEventListener('click', () => links.classList.toggle('open'));
        document.addEventListener('click', e => {
            if (!toggle.contains(e.target) && !links.contains(e.target)) {
                links.classList.remove('open');
            }
        });
    }
});
