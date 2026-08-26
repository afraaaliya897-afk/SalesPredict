const API_BASE_URL = 'http://localhost:8000';

const chatForm = document.getElementById('chat-form');
const questionInput = document.getElementById('question-input');
const sendButton = document.getElementById('send-button');
const messagesContainer = document.getElementById('messages');
const navItems = document.querySelectorAll('.nav-item');
const sections = document.querySelectorAll('.content-section');
const debugPanel = document.getElementById('debug-panel');
const debugToggle = document.getElementById('debug-toggle');
const debugContent = document.getElementById('debug-content');

const chatCharts = {};

navItems.forEach(item => {
    item.addEventListener('click', () => {
        const sectionId = item.dataset.section;
        navItems.forEach(nav => nav.classList.remove('active'));
        item.classList.add('active');
        sections.forEach(section => {
            section.classList.remove('active');
            if (section.id === `${sectionId}-section`) {
                section.classList.add('active');
            }
        });
    });
});

debugToggle.addEventListener('click', () => {
    debugPanel.classList.toggle('hidden');
});

chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    const question = questionInput.value.trim();
    if (!question) return;

    addMessage('user', question);
    questionInput.value = '';
    setInputState(false);
    const loadingId = addLoadingMessage();

    try {
        const response = await fetch(`${API_BASE_URL}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question })
        });

        if (!response.ok) {
            throw new Error(`API error: ${response.status}`);
        }

        const data = await response.json();
        removeMessage(loadingId);

        if (data.error) {
            addMessage('error', data.error);
        } else {
            addAssistantResult(data);
        }

        if (data.debug) {
            updateDebugPanel(data.debug);
        }
    } catch (error) {
        removeMessage(loadingId);
        addMessage('error', `Failed to get response: ${error.message}. Make sure the backend server is running.`);
    } finally {
        setInputState(true);
        questionInput.focus();
    }
});

function addMessage(type, content) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${type}-message`;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';

    const paragraphs = content.split('\n\n').filter(p => p.trim());
    paragraphs.forEach(para => {
        const p = document.createElement('p');
        p.textContent = para;
        contentDiv.appendChild(p);
    });

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

function addAssistantResult(data) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant-message';

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';

    const text = data.answer_text || data.answer || '';
    text.split('\n\n').filter(p => p.trim()).forEach(para => {
        const p = document.createElement('p');
        p.textContent = para;
        contentDiv.appendChild(p);
    });

    const chartType = data.chart_type;
    const tableData = data.table_data || [];

    if (chartType === 'stat' && tableData.length) {
        const stat = document.createElement('div');
        stat.className = 'chat-stat';
        const label = document.createElement('div');
        label.className = 'chat-stat-label';
        label.textContent = data.metric_label || 'Value';
        const value = document.createElement('div');
        value.className = 'chat-stat-value';
        const n = tableData[0].metric_value;
        value.textContent = typeof n === 'number' ? n.toLocaleString() : String(n ?? '—');
        stat.appendChild(label);
        stat.appendChild(value);
        contentDiv.appendChild(stat);
        contentDiv.appendChild(buildTable(tableData, data.dimension_label, data.metric_label, true));
    } else if ((chartType === 'bar' || chartType === 'line') && data.chart_data) {
        const card = document.createElement('div');
        card.className = 'chat-chart-card';

        const wrap = document.createElement('div');
        wrap.className = 'chat-chart-wrap';
        const canvas = document.createElement('canvas');
        const chartId = `chart-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        canvas.id = chartId;
        wrap.appendChild(canvas);
        card.appendChild(wrap);

        const tableWrap = buildTable(tableData, data.dimension_label, data.metric_label, false);
        tableWrap.style.display = 'none';
        card.appendChild(tableWrap);

        const actions = document.createElement('div');
        actions.className = 'chat-chart-actions';

        const tableBtn = document.createElement('button');
        tableBtn.type = 'button';
        tableBtn.textContent = 'View as table';
        tableBtn.addEventListener('click', () => {
            const showing = tableWrap.style.display !== 'none';
            tableWrap.style.display = showing ? 'none' : 'block';
            wrap.style.display = showing ? 'block' : 'none';
            tableBtn.textContent = showing ? 'View as table' : 'View as chart';
        });

        const downloadBtn = document.createElement('button');
        downloadBtn.type = 'button';
        downloadBtn.textContent = 'Download chart';
        downloadBtn.addEventListener('click', () => downloadChart(card, chartId));

        actions.appendChild(tableBtn);
        actions.appendChild(downloadBtn);
        card.appendChild(actions);
        contentDiv.appendChild(card);

        requestAnimationFrame(() => {
            renderChatChart(canvas, chartType, data.chart_data, data.metric_label || 'Value');
        });
    }

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

function buildTable(rows, dimensionLabel, metricLabel, visible) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-table-wrap';
    if (!visible) wrap.style.display = 'none';

    const table = document.createElement('table');
    table.className = 'chat-result-table';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');

    const keys = rows.length ? Object.keys(rows[0]) : ['metric_value'];
    keys.forEach(key => {
        const th = document.createElement('th');
        if (key === 'metric_value') th.textContent = metricLabel || 'Value';
        else th.textContent = dimensionLabel || key;
        headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    rows.forEach(row => {
        const tr = document.createElement('tr');
        keys.forEach(key => {
            const td = document.createElement('td');
            const val = row[key];
            if (typeof val === 'number') {
                td.className = 'num';
                td.textContent = val.toLocaleString();
            } else {
                td.textContent = val == null ? '—' : String(val);
            }
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
}

function renderChatChart(canvas, chartType, chartData, metricLabel) {
    if (typeof Chart === 'undefined') {
        canvas.replaceWith(Object.assign(document.createElement('p'), {
            textContent: 'Chart library failed to load. Use View as table to see the numbers.'
        }));
        return;
    }

    const labels = chartData.labels || [];
    const values = chartData.values || [];
    const isBar = chartType === 'bar';

    const chart = new Chart(canvas, {
        type: isBar ? 'bar' : 'line',
        data: {
            labels,
            datasets: [{
                label: metricLabel,
                data: values,
                backgroundColor: isBar ? 'rgba(59, 130, 246, 0.7)' : 'rgba(59, 130, 246, 0.12)',
                borderColor: '#3B82F6',
                borderWidth: 2,
                borderRadius: isBar ? 4 : 0,
                tension: 0.2,
                pointRadius: isBar ? 0 : 3,
                fill: !isBar,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: isBar ? 'y' : 'x',
            plugins: {
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: isBar,
                    callbacks: isBar ? {
                        label: (ctx) => `${metricLabel}: ${Number(ctx.raw).toLocaleString()}`
                    } : undefined
                }
            },
            interaction: {
                mode: 'index',
                axis: isBar ? 'y' : 'x',
                intersect: false
            },
            scales: {
                x: {
                    beginAtZero: isBar,
                    grid: { color: 'rgba(0,0,0,0.05)', drawBorder: false },
                    ticks: { font: { size: 11 }, color: '#6B7280' },
                    title: isBar ? { display: true, text: metricLabel, color: '#6B7280', font: { size: 11 } } : undefined
                },
                y: {
                    beginAtZero: !isBar,
                    grid: { color: isBar ? 'transparent' : 'rgba(0,0,0,0.05)', drawBorder: false },
                    ticks: { font: { size: 11 }, color: '#6B7280' }
                }
            }
        }
    });
    chatCharts[canvas.id] = chart;
}

function downloadChart(card, chartId) {
    const chart = chatCharts[chartId];
    const stamp = new Date().toISOString().slice(0, 10);
    const filename = `sales-chart-${stamp}.png`;

    const trigger = (href) => {
        const a = document.createElement('a');
        a.href = href;
        a.download = filename;
        a.click();
    };

    if (typeof html2canvas === 'function') {
        html2canvas(card.querySelector('.chat-chart-wrap') || card, {
            backgroundColor: '#ffffff',
            scale: 2
        }).then(canvas => trigger(canvas.toDataURL('image/png')));
        return;
    }

    if (chart && typeof chart.toBase64Image === 'function') {
        trigger(chart.toBase64Image('image/png', 1));
        return;
    }

    alert('Chart download is not available in this browser.');
}

function addLoadingMessage() {
    const id = `loading-${Date.now()}`;
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant-message loading-message';
    messageDiv.id = id;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';

    const loadingText = document.createElement('span');
    loadingText.textContent = 'Thinking';

    const loadingDots = document.createElement('div');
    loadingDots.className = 'loading-dots';
    loadingDots.innerHTML = '<span></span><span></span><span></span>';

    contentDiv.appendChild(loadingText);
    contentDiv.appendChild(loadingDots);
    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    return id;
}

function removeMessage(id) {
    const element = document.getElementById(id);
    if (element) element.remove();
}

function setInputState(enabled) {
    questionInput.disabled = !enabled;
    sendButton.disabled = !enabled;
}

function updateDebugPanel(debug) {
    if (!debug) return;

    let html = '';
    const plan = debug.plan_used || debug.query_plan;
    html += `
        <div class="debug-section">
            <div class="debug-section-title">Query plan</div>
            <div class="debug-badge ${debug.chart_type ? 'success' : 'info'}">
                ${debug.chart_type || plan?.metric || 'N/A'}
            </div>
            ${plan ? `<div class="debug-code">${escapeHtml(JSON.stringify(plan, null, 2))}</div>` : ''}
        </div>
    `;

    if (debug.sql_query) {
        html += `
            <div class="debug-section">
                <div class="debug-section-title">Generated SQL</div>
                <div class="debug-code">${escapeHtml(debug.sql_query)}</div>
            </div>
        `;
    }

    if (debug.execution) {
        html += `
            <div class="debug-section">
                <div class="debug-section-title">Execution</div>
                <div class="debug-metric">
                    <span class="debug-metric-label">Rows returned</span>
                    <span class="debug-metric-value">${debug.execution.rows_returned}</span>
                </div>
                ${debug.execution_time_ms ? `
                    <div class="debug-metric">
                        <span class="debug-metric-label">Execution time</span>
                        <span class="debug-metric-value">${debug.execution_time_ms}ms</span>
                    </div>
                ` : ''}
            </div>
        `;
    }

    debugContent.innerHTML = html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function checkApiHealth() {
    try {
        const response = await fetch(`${API_BASE_URL}/health`);
        if (response.ok) console.log('API connection successful');
    } catch (error) {
        console.warn('API not available:', error.message);
    }
}

checkApiHealth();
