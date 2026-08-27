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

const FORECAST_MODEL_STYLES = {
    prophet: { color: '#2563EB', fill: 'rgba(37, 99, 235, 0.14)' },
    moving_average: { color: '#D97706', fill: 'rgba(217, 119, 6, 0.14)' },
    seasonal_naive: { color: '#7C3AED', fill: 'rgba(124, 58, 237, 0.14)' },
};

const FORECAST_MODEL_LABELS = {
    prophet: 'Prophet',
    moving_average: 'Moving average',
    seasonal_naive: 'Seasonal naive',
};

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
    } else if ((chartType === 'bar' || chartType === 'line' || chartType === 'forecast') && data.chart_data) {
        const card = document.createElement('div');
        card.className = 'chat-chart-card';

        const wrap = document.createElement('div');
        wrap.className = chartType === 'forecast' ? 'chat-chart-wrap forecast' : 'chat-chart-wrap';
        const canvas = document.createElement('canvas');
        const chartId = `chart-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        canvas.id = chartId;

        if (chartType === 'forecast') {
            const chartData = data.chart_data;
            chartData.view = 'all';
            const models = chartData.models || [];
            const winnerName = chartData.model || 'Unknown';
            const selection = chartData.selection || '';

            const modelBar = document.createElement('div');
            modelBar.className = 'forecast-model-bar';

            const badge = document.createElement('div');
            badge.className = 'forecast-model-selected';
            badge.textContent = `All 3 models on the chart · best backtest: ${winnerName}`;
            modelBar.appendChild(badge);

            const why = document.createElement('div');
            why.className = 'forecast-model-why';
            why.textContent = selection
                ? `${selection}. Click a card to focus that line; click Compare all to see every model.`
                : 'Click a card to focus that line; click Compare all to see every model.';
            modelBar.appendChild(why);

            const list = document.createElement('div');
            list.className = 'forecast-model-scores';

            const chips = [];
            const makeChip = (view, title, wapeText, extraClass) => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = `forecast-model-chip ${extraClass || ''}`.trim();
                chip.dataset.view = view;
                const nameEl = document.createElement('span');
                nameEl.className = 'forecast-chip-name';
                nameEl.textContent = title;
                const wapeEl = document.createElement('span');
                wapeEl.className = 'forecast-chip-wape';
                wapeEl.textContent = wapeText;
                chip.appendChild(nameEl);
                chip.appendChild(wapeEl);
                chips.push(chip);
                list.appendChild(chip);
                return chip;
            };

            makeChip('all', 'Compare all', `${Math.max(models.length, 1)} models`, 'compare active');
            models.forEach((m) => {
                const wape = m.wape == null ? 'n/a' : `${(m.wape * 100).toFixed(1)}%`;
                const label = FORECAST_MODEL_LABELS[m.id] || m.name;
                const extra = `${m.id}${m.winner ? ' winner' : ''}`;
                const chip = makeChip(m.id, label, wape, extra);
                const meta = document.createElement('span');
                meta.className = 'forecast-chip-meta';
                meta.textContent = m.winner ? 'WAPE · best' : 'WAPE';
                chip.appendChild(meta);
            });
            modelBar.appendChild(list);
            card.appendChild(modelBar);

            wrap.appendChild(canvas);
            card.appendChild(wrap);

            const tableHost = document.createElement('div');
            let tableWrap = buildTable(
                forecastTableFromChart(chartData, 'all'),
                data.dimension_label,
                data.metric_label,
                false
            );
            tableHost.appendChild(tableWrap);
            card.appendChild(tableHost);

            const applyView = (view) => {
                chartData.view = view;
                chips.forEach((chip) => {
                    chip.classList.toggle('active', chip.dataset.view === view);
                });
                if (view === 'all') {
                    badge.textContent = `All 3 models on the chart · best backtest: ${winnerName}`;
                } else {
                    const focused = models.find((m) => m.id === view);
                    const name = focused ? (FORECAST_MODEL_LABELS[focused.id] || focused.name) : view;
                    badge.textContent = focused && focused.winner
                        ? `Showing ${name} (best backtest)`
                        : `Showing ${name}`;
                }
                const existing = chatCharts[chartId];
                if (existing) existing.destroy();
                renderChatChart(canvas, 'forecast', chartData, data.metric_label || 'Value');
                const nextTable = buildTable(
                    forecastTableFromChart(chartData, view),
                    data.dimension_label,
                    data.metric_label,
                    tableWrap.style.display !== 'none'
                );
                tableHost.replaceChildren(nextTable);
                tableWrap = nextTable;
            };

            chips.forEach((chip) => {
                chip.addEventListener('click', () => applyView(chip.dataset.view));
            });

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
                renderChatChart(canvas, chartType, chartData, data.metric_label || 'Value');
            });
        } else {
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
    }

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

function headerForKey(key, dimensionLabel, metricLabel) {
    const labels = {
        metric_value: metricLabel || 'Value',
        date: 'Date',
        actual: 'Actual',
        quantity: 'Forecast',
        lower: 'Lower',
        upper: 'Upper',
        prophet: 'Prophet',
        moving_average: 'Moving average',
        seasonal_naive: 'Seasonal naive',
    };
    if (labels[key]) return labels[key];
    return dimensionLabel || key;
}

function formatTableCell(val) {
    if (val == null || val === '') return '—';
    if (typeof val === 'number') {
        return { numeric: true, text: val.toLocaleString(undefined, { maximumFractionDigits: 1 }) };
    }
    if (typeof val === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(val)) {
        return { numeric: false, text: val.slice(0, 10) };
    }
    if (val instanceof Date && !Number.isNaN(val.getTime())) {
        return { numeric: false, text: val.toISOString().slice(0, 10) };
    }
    return { numeric: false, text: String(val) };
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
        th.textContent = headerForKey(key, dimensionLabel, metricLabel);
        headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    rows.forEach(row => {
        const tr = document.createElement('tr');
        keys.forEach(key => {
            const td = document.createElement('td');
            const cell = formatTableCell(row[key]);
            if (cell.numeric) td.className = 'num';
            td.textContent = cell.text;
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
}

function monthLabel(iso) {
    const d = new Date(String(iso).slice(0, 10) + 'T00:00:00');
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString('en-US', { month: 'short', year: 'numeric' });
}

function weekLabel(iso) {
    const d = new Date(String(iso).slice(0, 10) + 'T00:00:00');
    if (Number.isNaN(d.getTime())) return String(iso);
    const day = (d.getDay() + 6) % 7;
    d.setDate(d.getDate() - day);
    return d.toLocaleString('en-US', { month: 'short', day: 'numeric' });
}

function bucketKey(iso, grain) {
    const raw = String(iso).slice(0, 10);
    if (grain === 'month') return raw.slice(0, 7);
    if (grain === 'week') {
        const d = new Date(raw + 'T00:00:00');
        const day = (d.getDay() + 6) % 7;
        d.setDate(d.getDate() - day);
        return d.toISOString().slice(0, 10);
    }
    return raw;
}

function sumBucket(values) {
    const nums = values.filter((v) => v != null && !Number.isNaN(Number(v)));
    if (!nums.length) return null;
    return nums.reduce((a, b) => a + Number(b), 0);
}

function forecastTableFromChart(chartData, view) {
    const labels = chartData.labels || [];
    const hist = chartData.historical || [];
    const models = chartData.models || [];
    const focused = view && view !== 'all' ? models.find((m) => m.id === view) : null;
    const num = (arr, i) => {
        if (!arr || i >= arr.length) return null;
        const v = arr[i];
        return v == null || v === '' ? null : Number(v);
    };
    return labels.map((label, i) => {
        const row = { date: String(label), actual: num(hist, i) };
        if (focused) {
            row.quantity = num(focused.forecast, i);
            row.lower = num(focused.lower, i);
            row.upper = num(focused.upper, i);
        } else if (models.length) {
            models.forEach((m) => {
                row[m.id] = num(m.forecast, i);
            });
        } else {
            row.quantity = num(chartData.forecast, i);
            row.lower = num(chartData.lower, i);
            row.upper = num(chartData.upper, i);
        }
        return row;
    });
}

function prepareForecastSeries(chartData) {
    const labels = chartData.labels || [];
    const historical = chartData.historical || [];
    const models = (chartData.models && chartData.models.length)
        ? chartData.models.map((m) => ({
            id: m.id,
            name: m.name,
            winner: !!m.winner,
            forecast: m.forecast || [],
            lower: m.lower || [],
            upper: m.upper || [],
        }))
        : [{
            id: 'selected',
            name: chartData.model || 'Forecast',
            winner: true,
            forecast: chartData.forecast || [],
            lower: chartData.lower || [],
            upper: chartData.upper || [],
        }];
    const n = labels.length;

    const asDisplay = (iso, grain) => {
        if (grain === 'month') return monthLabel(String(iso).slice(0, 7) + '-01');
        if (grain === 'week') return weekLabel(iso);
        const d = new Date(String(iso).slice(0, 10) + 'T00:00:00');
        return Number.isNaN(d.getTime())
            ? String(iso)
            : d.toLocaleString('en-US', { month: 'short', day: 'numeric' });
    };

    if (chartData.grain === 'month') {
        return {
            labels: labels.map((iso) => asDisplay(iso, 'month')),
            historical,
            models,
            grain: 'month',
        };
    }

    const grain = n >= 120 ? 'month' : n >= 50 ? 'week' : 'day';
    if (grain === 'day') {
        return {
            labels: labels.map((iso) => asDisplay(iso, 'day')),
            historical,
            models,
            grain,
        };
    }

    const order = [];
    const buckets = new Map();
    labels.forEach((iso, i) => {
        const key = bucketKey(iso, grain);
        if (!buckets.has(key)) {
            buckets.set(key, {
                hist: [],
                models: models.map(() => ({ fc: [], lo: [], hi: [] })),
            });
            order.push(key);
        }
        const b = buckets.get(key);
        b.hist.push(historical[i]);
        models.forEach((m, mi) => {
            b.models[mi].fc.push(m.forecast[i]);
            b.models[mi].lo.push(m.lower[i]);
            b.models[mi].hi.push(m.upper[i]);
        });
    });

    const outHist = [];
    const outModels = models.map((m) => ({ ...m, forecast: [], lower: [], upper: [] }));
    const outLabels = [];
    order.forEach((key) => {
        const b = buckets.get(key);
        outLabels.push(grain === 'month' ? monthLabel(key + '-01') : weekLabel(key));
        outHist.push(sumBucket(b.hist));
        outModels.forEach((m, mi) => {
            m.forecast.push(sumBucket(b.models[mi].fc));
            const lo = sumBucket(b.models[mi].lo);
            const hi = sumBucket(b.models[mi].hi);
            m.lower.push(lo == null ? null : Math.max(0, lo));
            m.upper.push(hi == null ? null : Math.max(0, hi));
        });
    });
    return { labels: outLabels, historical: outHist, models: outModels, grain };
}

function renderChatChart(canvas, chartType, chartData, metricLabel) {
    if (typeof Chart === 'undefined') {
        canvas.replaceWith(Object.assign(document.createElement('p'), {
            textContent: 'Chart library failed to load. Use View as table to see the numbers.'
        }));
        return;
    }

    const labels = chartData.labels || [];
    
    if (chartType === 'forecast') {
        const series = prepareForecastSeries(chartData);
        const view = chartData.view || 'all';
        const yTitle = series.grain === 'month'
            ? 'Units per month'
            : series.grain === 'week'
                ? 'Units per week'
                : 'Units per day';
        const showPoints = series.labels.length <= 24;
        const focused = view !== 'all' ? series.models.find((m) => m.id === view) : null;

        const datasets = [{
            label: 'Actual demand',
            data: series.historical,
            borderColor: '#9CA3AF',
            backgroundColor: '#9CA3AF',
            borderWidth: 1.75,
            tension: 0.15,
            pointRadius: showPoints ? 3 : 0,
            pointHoverRadius: 4,
            pointBackgroundColor: '#9CA3AF',
            pointBorderColor: '#fff',
            pointBorderWidth: 1,
            fill: false,
            spanGaps: false,
        }];

        series.models.forEach((m) => {
            const style = FORECAST_MODEL_STYLES[m.id] || { color: '#2563EB', fill: 'rgba(37, 99, 235, 0.14)' };
            const isFocus = focused ? m.id === focused.id : true;
            const faded = focused && m.id !== focused.id;
            datasets.push({
                label: FORECAST_MODEL_LABELS[m.id] || m.name,
                data: m.forecast,
                borderColor: style.color,
                backgroundColor: style.color,
                borderWidth: isFocus ? 2.6 : 1.5,
                borderDash: faded ? [5, 4] : [],
                tension: 0.15,
                pointRadius: showPoints && isFocus ? 4 : 0,
                pointHoverRadius: 5,
                pointBackgroundColor: style.color,
                pointBorderColor: '#fff',
                pointBorderWidth: 1,
                fill: false,
                spanGaps: false,
                hidden: false,
            });
            if (focused && m.id === focused.id) {
                datasets.push({
                    label: 'Forecast range',
                    data: m.upper,
                    borderColor: 'transparent',
                    backgroundColor: style.fill,
                    borderWidth: 0,
                    tension: 0.15,
                    pointRadius: 0,
                    fill: '+1',
                });
                datasets.push({
                    label: 'Lower Bound',
                    data: m.lower,
                    borderColor: 'transparent',
                    backgroundColor: style.fill,
                    borderWidth: 0,
                    tension: 0.15,
                    pointRadius: 0,
                    fill: false,
                });
            }
        });

        const title = focused
            ? `Demand forecast — ${FORECAST_MODEL_LABELS[focused.id] || focused.name}`
            : 'Demand forecast — all 3 models';

        const chart = new Chart(canvas, {
            type: 'line',
            data: {
                labels: series.labels,
                datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        align: 'end',
                        labels: {
                            boxWidth: 14,
                            boxHeight: 3,
                            usePointStyle: false,
                            font: { size: 11 },
                            color: '#4B5563',
                            filter: (item) => item.text !== 'Lower Bound' && item.text !== 'Forecast range',
                        }
                    },
                    title: {
                        display: true,
                        text: title,
                        color: '#111827',
                        font: { size: 14, weight: '600' },
                        padding: { bottom: 4 },
                        align: 'start'
                    },
                    tooltip: {
                        enabled: true,
                        mode: 'index',
                        intersect: false,
                        backgroundColor: '#111827',
                        titleColor: '#F9FAFB',
                        bodyColor: '#E5E7EB',
                        displayColors: true,
                        callbacks: {
                            label: (ctx) => {
                                if (ctx.dataset.label === 'Lower Bound' || ctx.dataset.label === 'Forecast range') {
                                    return null;
                                }
                                const value = ctx.raw;
                                if (value === null || value === undefined) return null;
                                return `${ctx.dataset.label}: ${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false, drawBorder: false },
                        ticks: {
                            font: { size: 11 },
                            color: '#6B7280',
                            maxRotation: 0,
                            autoSkip: true,
                            maxTicksLimit: series.grain === 'month' ? 12 : 10
                        }
                    },
                    y: {
                        beginAtZero: true,
                        border: { display: false },
                        grid: {
                            color: 'rgba(0,0,0,0.06)',
                            borderDash: [4, 4],
                            drawBorder: false
                        },
                        ticks: {
                            font: { size: 11 },
                            color: '#6B7280',
                            callback: (value) => Number(value).toLocaleString()
                        },
                        title: {
                            display: true,
                            text: yTitle,
                            color: '#6B7280',
                            font: { size: 11 }
                        }
                    }
                }
            }
        });

        chatCharts[canvas.id] = chart;
        return;
    }

    // Original logic for bar/line charts
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
    loadingText.textContent = 'Planning query';

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
