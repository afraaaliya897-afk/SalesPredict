const API_BASE_URL = 'http://localhost:8001';

const chatForm = document.getElementById('chat-form');
const questionInput = document.getElementById('question-input');
const sendButton = document.getElementById('send-button');
const messagesContainer = document.getElementById('messages');
const navItems = document.querySelectorAll('.nav-item');
const sections = document.querySelectorAll('.content-section');
const debugPanel = document.getElementById('debug-panel');
const debugToggle = document.getElementById('debug-toggle');
const debugContent = document.getElementById('debug-content');
const modelSelect = document.getElementById('model-select');
const MODEL_STORAGE_KEY = 'salesChatModel';

const chatCharts = {};

const PLANNING_FORECAST_COLOR = '#2563EB';
const PLANNING_ACTUAL_COLOR = '#9CA3AF';
const PLANNING_BAND = 'rgba(37, 99, 235, 0.16)';

const FORECAST_MODEL_STYLES = {
    prophet: { color: '#2563EB', fill: 'rgba(37, 99, 235, 0.16)' },
    seasonal_naive_yearly: { color: '#7C3AED', fill: 'rgba(124, 58, 237, 0.14)' },
    seasonal_naive: { color: '#7C3AED', fill: 'rgba(124, 58, 237, 0.14)' },
    moving_average: { color: '#D97706', fill: 'rgba(217, 119, 6, 0.14)' },
    ets: { color: '#0D9488', fill: 'rgba(13, 148, 136, 0.14)' },
};

const FORECAST_MODEL_LABELS = {
    prophet: 'Prophet',
    seasonal_naive_yearly: 'Baseline (last year)',
    seasonal_naive: 'Baseline (last week)',
    moving_average: 'Moving average',
    ets: 'ETS',
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
            body: JSON.stringify({
                question,
                model: modelSelect && modelSelect.value ? modelSelect.value : undefined,
            })
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

    // Add collapsible thinking section if available (DeepSeek reasoning)
    if (data.thinking) {
        const thinkingCard = document.createElement('details');
        thinkingCard.className = 'thinking-card';
        thinkingCard.open = true;  // Auto-expand to show thinking
        const summary = document.createElement('summary');
        summary.className = 'thinking-summary';
        summary.innerHTML = '<span>💭 Model Thoughts</span><span class="thinking-toggle"></span>';
        const thinkingContent = document.createElement('pre');
        thinkingContent.className = 'thinking-content';
        thinkingContent.textContent = data.thinking;
        thinkingCard.appendChild(summary);
        thinkingCard.appendChild(thinkingContent);
        contentDiv.appendChild(thinkingCard);
    }

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
    } else if ((chartType === 'bar' || chartType === 'line' || chartType === 'pie' || chartType === 'forecast') && data.chart_data) {
        const card = document.createElement('div');
        card.className = 'chat-chart-card';

        const wrap = document.createElement('div');
        wrap.className = chartType === 'forecast'
            ? 'chat-chart-wrap forecast'
            : (chartType === 'pie' ? 'chat-chart-wrap pie' : 'chat-chart-wrap');
        const canvas = document.createElement('canvas');
        const chartId = `chart-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        canvas.id = chartId;

        if (chartType === 'forecast') {
            const chartData = data.chart_data;
            const models = chartData.models || [];
            const winner = models.find((m) => m.winner) || models[0];
            chartData.view = chartData.view || (winner && winner.id) || 'all';

            const heading = document.createElement('div');
            heading.className = 'planning-heading';
            const title = document.createElement('div');
            title.className = 'planning-title';
            title.textContent = chartData.scope_label && chartData.scope_label !== 'all sold items'
                ? `Forecast — ${chartData.scope_label}`
                : 'Demand forecast';
            const caption = document.createElement('div');
            caption.className = 'planning-caption';
            heading.appendChild(title);
            heading.appendChild(caption);
            card.appendChild(heading);

            const modelBar = document.createElement('div');
            modelBar.className = 'forecast-model-bar';
            const why = document.createElement('div');
            why.className = 'forecast-model-why';
            why.textContent = chartData.selection
                ? `${chartData.selection}. Lowest WAPE among production models wins; baseline is comparison only.`
                : 'Lowest WAPE among production models wins.';
            modelBar.appendChild(why);

            const list = document.createElement('div');
            list.className = 'forecast-model-scores';
            const chips = [];
            const makeChip = (view, name, wapeText, extraClass) => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = `forecast-model-chip ${extraClass || ''}`.trim();
                chip.dataset.view = view;
                const nameEl = document.createElement('span');
                nameEl.className = 'forecast-chip-name';
                nameEl.textContent = name;
                const wapeEl = document.createElement('span');
                wapeEl.className = 'forecast-chip-wape';
                wapeEl.textContent = wapeText;
                chip.appendChild(nameEl);
                chip.appendChild(wapeEl);
                chips.push(chip);
                list.appendChild(chip);
                return chip;
            };
            makeChip('all', 'Compare all', `${Math.max(models.length, 1)} models`, 'compare');
            models.forEach((m) => {
                const wape = m.wape == null ? 'n/a' : `${(m.wape * 100).toFixed(1)}%`;
                const extra = `${m.id}${m.winner ? ' winner' : ''}${m.baseline ? ' baseline' : ''}`;
                const chip = makeChip(m.id, FORECAST_MODEL_LABELS[m.id] || m.name, wape, extra);
                const meta = document.createElement('span');
                meta.className = 'forecast-chip-meta';
                meta.textContent = m.winner ? 'WAPE · selected' : (m.baseline ? 'WAPE · baseline' : 'WAPE');
                chip.appendChild(meta);
            });
            modelBar.appendChild(list);
            card.appendChild(modelBar);

            wrap.appendChild(canvas);
            card.appendChild(wrap);

            const tableCaption = document.createElement('div');
            tableCaption.className = 'planning-table-caption';
            tableCaption.textContent = chartData.grain === 'day' ? 'Daily detail (units)' : 'Monthly detail (units)';
            card.appendChild(tableCaption);
            const tableHost = document.createElement('div');
            card.appendChild(tableHost);

            const insightsHost = document.createElement('div');
            card.appendChild(insightsHost);

            const setCaption = (view) => {
                if (view === 'all') {
                    caption.textContent = 'Gray = prior-year actuals. Colored = model forecasts.';
                    return;
                }
                const focused = models.find((m) => m.id === view);
                const name = focused
                    ? (FORECAST_MODEL_LABELS[focused.id] || focused.name)
                    : 'Forecast';
                if (focused && focused.baseline) {
                    caption.textContent = `${name}: same months last year (comparison baseline).`;
                } else {
                    caption.textContent = `${name} vs prior-year actuals. Shaded band = confidence range.`;
                }
            };

            const renderInsights = (view) => {
                insightsHost.replaceChildren();
                const focused = view !== 'all' ? models.find((m) => m.id === view) : winner;
                const basis = chartData.basis || [];
                const drivers = chartData.drivers || [];
                const fallback = chartData.insights || [];
                const bits = [];
                if (focused && focused.peak && focused.peak.label) {
                    bits.push(`${focused.peak.month}: ${focused.peak.label}`);
                }
                const methodBits = basis.length ? basis : [];
                const whyBits = drivers.length ? drivers : fallback.filter((t) => !methodBits.includes(t));
                const uniqueMethod = [...new Set(methodBits.filter(Boolean))];
                const uniqueWhy = [...new Set([...bits, ...whyBits].filter(Boolean))];
                if (!uniqueMethod.length && !uniqueWhy.length) return;

                if (uniqueMethod.length) {
                    const h = document.createElement('p');
                    h.className = 'planning-insights-title';
                    h.textContent = 'On what basis';
                    insightsHost.appendChild(h);
                    const ul = document.createElement('ul');
                    ul.className = 'planning-insights';
                    uniqueMethod.slice(0, 4).forEach((text) => {
                        const li = document.createElement('li');
                        li.textContent = text;
                        ul.appendChild(li);
                    });
                    insightsHost.appendChild(ul);
                }
                if (uniqueWhy.length) {
                    const h = document.createElement('p');
                    h.className = 'planning-insights-title';
                    h.textContent = 'Why up / down vs last year';
                    insightsHost.appendChild(h);
                    const ul = document.createElement('ul');
                    ul.className = 'planning-insights';
                    uniqueWhy.slice(0, 5).forEach((text) => {
                        const li = document.createElement('li');
                        li.textContent = text;
                        ul.appendChild(li);
                    });
                    insightsHost.appendChild(ul);
                }
            };

            const applyView = (view) => {
                chartData.view = view;
                chips.forEach((chip) => chip.classList.toggle('active', chip.dataset.view === view));
                if (view !== 'all') {
                    const focused = models.find((m) => m.id === view);
                    if (focused) {
                        chartData.forecast = focused.forecast;
                        chartData.lower = focused.lower;
                        chartData.upper = focused.upper;
                        chartData.peak = focused.peak;
                        chartData.forecast_label = focused.forecast_label;
                        if (chartData.planning_table) {
                            chartData.planning_table.forecast = focused.forecast;
                            chartData.planning_table.yoy = focused.yoy;
                            chartData.planning_table.forecast_label = focused.table_forecast_label || 'Forecast';
                        }
                    }
                }
                setCaption(view);
                const existing = chatCharts[chartId];
                if (existing) existing.destroy();
                renderChatChart(canvas, 'forecast', chartData, data.metric_label || 'Units');
                tableHost.replaceChildren(buildPlanningTable(chartData));
                renderInsights(view);
            };

            chips.forEach((chip) => {
                chip.addEventListener('click', () => applyView(chip.dataset.view));
            });

            const actions = document.createElement('div');
            actions.className = 'chat-chart-actions';
            const downloadBtn = document.createElement('button');
            downloadBtn.type = 'button';
            downloadBtn.textContent = 'Download chart';
            downloadBtn.addEventListener('click', () => downloadChart(card, chartId));
            actions.appendChild(downloadBtn);
            card.appendChild(actions);
            contentDiv.appendChild(card);

            requestAnimationFrame(() => applyView(chartData.view));
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

    // Add "View SQL" button if debug data exists
    if (data.debug && (data.debug.sql_query || data.debug.plan_used)) {
        const sqlButton = document.createElement('button');
        sqlButton.type = 'button';
        sqlButton.className = 'view-sql-btn';
        sqlButton.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="16 18 22 12 16 6"></polyline>
                <polyline points="8 6 2 12 8 18"></polyline>
            </svg>
            View SQL
        `;
        sqlButton.addEventListener('click', () => {
            updateDebugPanel(data.debug);
            if (debugPanel.classList.contains('hidden')) {
                debugPanel.classList.remove('hidden');
            }
            debugPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        });
        contentDiv.appendChild(sqlButton);
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

function formatPlanningCell(val, isYoy) {
    if (val == null || val === '') return { text: '—', cls: 'num' };
    if (isYoy) {
        const text = String(val);
        const cls = text.startsWith('+') ? 'num yoy-up' : text.startsWith('-') ? 'num yoy-down' : 'num';
        return { text, cls };
    }
    if (typeof val === 'number') {
        return { text: val.toLocaleString(undefined, { maximumFractionDigits: 0 }), cls: 'num' };
    }
    return { text: String(val), cls: 'num' };
}

function buildPlanningTable(chartData) {
    const tableSpec = chartData.planning_table || {};
    const columns = tableSpec.columns || chartData.labels || [];
    const wrap = document.createElement('div');
    wrap.className = 'chat-table-wrap planning-table-wrap';
    const table = document.createElement('table');
    table.className = 'chat-result-table planning-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    const corner = document.createElement('th');
    corner.textContent = 'Month';
    headRow.appendChild(corner);
    columns.forEach((col) => {
        const th = document.createElement('th');
        th.textContent = col;
        th.className = 'num';
        headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const rows = chartData.view === 'all' && (chartData.models || []).length
        ? [
            { label: tableSpec.actual_label || 'Actual', values: tableSpec.actual || chartData.actual || [], yoy: false },
            ...chartData.models.map((m) => ({
                label: FORECAST_MODEL_LABELS[m.id] || m.name,
                values: m.forecast || [],
                yoy: false,
            })),
        ]
        : [
            { label: tableSpec.actual_label || 'Actual', values: tableSpec.actual || chartData.actual || [], yoy: false },
            { label: tableSpec.forecast_label || 'Forecast', values: tableSpec.forecast || chartData.forecast || [], yoy: false },
            { label: 'YoY change', values: tableSpec.yoy || chartData.yoy_labels || [], yoy: true },
        ];
    const tbody = document.createElement('tbody');
    rows.forEach((row) => {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.scope = 'row';
        th.textContent = row.label;
        tr.appendChild(th);
        columns.forEach((_, i) => {
            const td = document.createElement('td');
            const cell = formatPlanningCell(row.values[i], row.yoy);
            td.className = cell.cls;
            td.textContent = cell.text;
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
}

const forecastPeakPlugin = {
    id: 'forecastPeakCallout',
    afterDatasetsDraw(chart, _args, opts) {
        const peak = opts && opts.peak;
        const datasetIndex = opts && opts.forecastDatasetIndex;
        if (!peak || peak.index == null || datasetIndex == null) return;
        const meta = chart.getDatasetMeta(datasetIndex);
        const pt = meta && meta.data ? meta.data[peak.index] : null;
        if (!pt || pt.skip) return;
        const { ctx, chartArea } = chart;
        const label = peak.label || 'Peak';
        ctx.save();
        ctx.strokeStyle = PLANNING_FORECAST_COLOR;
        ctx.fillStyle = PLANNING_FORECAST_COLOR;
        ctx.lineWidth = 1.5;
        const roomRight = chartArea.right - pt.x;
        const goRight = roomRight > 90;
        const x2 = goRight
            ? Math.min(pt.x + 42, chartArea.right - 8)
            : Math.max(pt.x - 42, chartArea.left + 8);
        const y2 = Math.max(pt.y - 32, chartArea.top + 10);
        ctx.beginPath();
        ctx.moveTo(pt.x, pt.y - 6);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(pt.x, pt.y - 1);
        ctx.lineTo(pt.x - 4, pt.y - 9);
        ctx.lineTo(pt.x + 4, pt.y - 9);
        ctx.closePath();
        ctx.fill();
        ctx.font = '600 12px "Segoe UI", system-ui, sans-serif';
        ctx.textAlign = goRight ? 'left' : 'right';
        ctx.textBaseline = 'bottom';
        ctx.fillText(label, goRight ? x2 + 4 : x2 - 4, y2 - 2);
        ctx.restore();
    }
};

function renderChatChart(canvas, chartType, chartData, metricLabel) {
    if (typeof Chart === 'undefined') {
        canvas.replaceWith(Object.assign(document.createElement('p'), {
            textContent: 'Chart library failed to load. Use View as table to see the numbers.'
        }));
        return;
    }

    const labels = chartData.labels || [];
    
    if (chartType === 'forecast') {
        const actual = chartData.actual || chartData.historical || [];
        const forecast = chartData.forecast || [];
        const lower = chartData.lower || [];
        const upper = chartData.upper || [];
        const showPoints = labels.length <= 18;
        const actualLabel = chartData.actual_label || 'Actual demand';
        const forecastLabel = chartData.forecast_label || chartData.model || 'Prediction model';
        const yTitle = chartData.y_title || (chartData.grain === 'month' ? 'Units per month' : 'Units');
        const compareAll = chartData.view === 'all' && (chartData.models || []).length > 1;

        if (compareAll) {
            const datasets = [{
                label: actualLabel,
                data: actual,
                borderColor: PLANNING_ACTUAL_COLOR,
                backgroundColor: PLANNING_ACTUAL_COLOR,
                borderWidth: 2,
                tension: 0.25,
                pointRadius: showPoints ? 3 : 0,
                pointHoverRadius: 5,
                pointBackgroundColor: PLANNING_ACTUAL_COLOR,
                pointBorderColor: '#fff',
                pointBorderWidth: 1,
                fill: false,
                spanGaps: false,
            }];
            chartData.models.forEach((m) => {
                const style = FORECAST_MODEL_STYLES[m.id] || { color: PLANNING_FORECAST_COLOR };
                datasets.push({
                    label: FORECAST_MODEL_LABELS[m.id] || m.name,
                    data: m.forecast,
                    borderColor: style.color,
                    backgroundColor: style.color,
                    borderWidth: m.winner ? 2.6 : 1.8,
                    tension: 0.25,
                    pointRadius: showPoints ? 3 : 0,
                    pointHoverRadius: 5,
                    pointBackgroundColor: style.color,
                    pointBorderColor: '#fff',
                    pointBorderWidth: 1,
                    fill: false,
                    spanGaps: false,
                });
            });
            const chart = new Chart(canvas, {
                type: 'line',
                data: { labels, datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    layout: { padding: { top: 8, right: 8, left: 4 } },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top',
                            align: 'end',
                            labels: { boxWidth: 14, boxHeight: 3, font: { size: 11 }, color: '#4B5563' }
                        },
                        title: { display: false },
                        tooltip: {
                            enabled: true,
                            mode: 'index',
                            intersect: false,
                            backgroundColor: '#111827',
                            titleColor: '#F9FAFB',
                            bodyColor: '#E5E7EB',
                            callbacks: {
                                label: (ctx) => {
                                    if (ctx.raw == null) return null;
                                    return `${ctx.dataset.label}: ${Number(ctx.raw).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { display: false, drawBorder: false },
                            ticks: { font: { size: 11 }, color: '#6B7280', maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }
                        },
                        y: {
                            beginAtZero: true,
                            border: { display: false },
                            grid: { color: 'rgba(0,0,0,0.07)', borderDash: [4, 4], drawBorder: false },
                            ticks: { font: { size: 11 }, color: '#6B7280', callback: (value) => Number(value).toLocaleString() },
                            title: { display: true, text: yTitle, color: '#6B7280', font: { size: 11 } }
                        }
                    }
                }
            });
            chatCharts[canvas.id] = chart;
            return;
        }

        const datasets = [
            {
                label: 'Forecast confidence range',
                data: upper,
                borderColor: 'transparent',
                backgroundColor: PLANNING_BAND,
                borderWidth: 0,
                tension: 0.25,
                pointRadius: 0,
                fill: '+1',
                spanGaps: false,
            },
            {
                label: 'Lower Bound',
                data: lower,
                borderColor: 'transparent',
                backgroundColor: PLANNING_BAND,
                borderWidth: 0,
                tension: 0.25,
                pointRadius: 0,
                fill: false,
                spanGaps: false,
            },
            {
                label: actualLabel,
                data: actual,
                borderColor: PLANNING_ACTUAL_COLOR,
                backgroundColor: PLANNING_ACTUAL_COLOR,
                borderWidth: 2,
                tension: 0.25,
                pointRadius: showPoints ? 3 : 0,
                pointHoverRadius: 5,
                pointBackgroundColor: PLANNING_ACTUAL_COLOR,
                pointBorderColor: '#fff',
                pointBorderWidth: 1,
                fill: false,
                spanGaps: false,
            },
            {
                label: forecastLabel,
                data: forecast,
                borderColor: PLANNING_FORECAST_COLOR,
                backgroundColor: PLANNING_FORECAST_COLOR,
                borderWidth: 2.6,
                tension: 0.25,
                pointRadius: showPoints ? 4 : 0,
                pointHoverRadius: 6,
                pointBackgroundColor: PLANNING_FORECAST_COLOR,
                pointBorderColor: '#fff',
                pointBorderWidth: 1.5,
                fill: false,
                spanGaps: false,
            },
        ];

        const legendOrder = [actualLabel, forecastLabel, 'Forecast confidence range'];
        const chart = new Chart(canvas, {
            type: 'line',
            plugins: [forecastPeakPlugin],
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                layout: { padding: { top: 28, right: 8, left: 4 } },
                plugins: {
                    forecastPeakCallout: {
                        peak: chartData.peak || null,
                        forecastDatasetIndex: 3,
                    },
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
                            filter: (item) => item.text !== 'Lower Bound',
                            sort: (a, b) => legendOrder.indexOf(a.text) - legendOrder.indexOf(b.text),
                        }
                    },
                    title: { display: false },
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
                                if (ctx.dataset.label === 'Lower Bound' || ctx.dataset.label === 'Forecast confidence range') {
                                    if (ctx.dataset.label === 'Forecast confidence range') {
                                        const i = ctx.dataIndex;
                                        const lo = lower[i];
                                        const hi = upper[i];
                                        if (lo == null || hi == null) return null;
                                        return `Confidence range: ${Number(lo).toLocaleString(undefined, { maximumFractionDigits: 0 })} – ${Number(hi).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
                                    }
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
                            maxTicksLimit: chartData.grain === 'month' ? 12 : 10
                        }
                    },
                    y: {
                        beginAtZero: true,
                        border: { display: false },
                        grid: {
                            color: 'rgba(0,0,0,0.07)',
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

    // Original logic for bar/line/pie charts
    const values = chartData.values || [];
    const isPie = chartType === 'pie';
    const isBar = chartType === 'bar' && !isPie;
    const isLine = chartType === 'line' && !isPie;

    // Power BI-inspired color palette (vibrant, professional, accessible)
    const pieColors = [
        '#0D9488', '#DC2626', '#D97706', '#2563EB', '#7C3AED',
        '#DB2777', '#059669', '#CA8A04', '#0891B2', '#4B5563',
        '#EA580C', '#4F46E5', '#16A34A', '#BE185D', '#0F766E',
    ];
    const sliceColors = values.map((_, i) => pieColors[i % pieColors.length]);

    let chartConfig;

    if (isPie) {
        // Power BI-style Pie chart configuration
        chartConfig = {
            type: 'pie',
            data: {
                labels,
                datasets: [{
                    label: metricLabel,
                    data: values,
                    backgroundColor: sliceColors,
                    borderColor: sliceColors,
                    borderWidth: 0,
                    spacing: 0,
                    offset: 0,
                    hoverOffset: 0,
                    hoverBorderWidth: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: {
                    animateRotate: true,
                    animateScale: false,
                    duration: 500,
                    easing: 'easeOutQuart'
                },
                layout: {
                    padding: 4
                },
                plugins: {
                    legend: {
                        display: true,
                        position: 'right',
                        align: 'center',
                        labels: {
                            boxWidth: 12,
                            boxHeight: 12,
                            borderRadius: 2,
                            useBorderRadius: true,
                            font: { 
                                size: 11, 
                                family: "'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif",
                                weight: '500'
                            },
                            color: '#374151',
                            padding: 8,
                            generateLabels: (chart) => {
                                const data = chart.data;
                                if (data.labels.length && data.datasets.length) {
                                    const total = data.datasets[0].data.reduce((a, b) => a + (Number(b) || 0), 0);
                                    return data.labels.map((label, i) => {
                                        const value = Number(data.datasets[0].data[i]) || 0;
                                        const percentage = total ? ((value / total) * 100).toFixed(1) : '0.0';
                                        const color = sliceColors[i];
                                        return {
                                            text: `${label} (${percentage}%)`,
                                            fillStyle: color,
                                            strokeStyle: color,
                                            lineWidth: 0,
                                            hidden: false,
                                            index: i
                                        };
                                    });
                                }
                                return [];
                            }
                        }
                    },
                    tooltip: {
                        enabled: true,
                        backgroundColor: 'rgba(0, 0, 0, 0.85)',
                        titleColor: '#FFFFFF',
                        titleFont: {
                            size: 13,
                            weight: '600',
                            family: "'Segoe UI', sans-serif"
                        },
                        bodyColor: '#E8E8E8',
                        bodyFont: {
                            size: 12,
                            family: "'Segoe UI', sans-serif"
                        },
                        padding: 12,
                        borderColor: 'rgba(255, 255, 255, 0.1)',
                        borderWidth: 1,
                        displayColors: true,
                        boxWidth: 12,
                        boxHeight: 12,
                        boxPadding: 6,
                        cornerRadius: 6,
                        callbacks: {
                            label: (ctx) => {
                                const label = ctx.label || '';
                                const value = ctx.raw || 0;
                                const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                                const percentage = ((value / total) * 100).toFixed(1);
                                return [
                                    `${label}`,
                                    `Value: ${Number(value).toLocaleString()}`,
                                    `Percentage: ${percentage}%`
                                ];
                            }
                        }
                    }
                }
            }
        };
    } else {
        // Power BI-style Bar/Line chart configuration
        const powerBIBlue = '#01B8AA';
        const powerBIBlueLight = 'rgba(1, 184, 170, 0.15)';
        
        chartConfig = {
            type: isBar ? 'bar' : 'line',
            data: {
                labels,
                datasets: [{
                    label: metricLabel,
                    data: values,
                    backgroundColor: isBar ? powerBIBlue : powerBIBlueLight,
                    borderColor: powerBIBlue,
                    borderWidth: isBar ? 0 : 2.5,
                    borderRadius: isBar ? 6 : 0,
                    tension: 0.35,
                    pointRadius: isBar ? 0 : 4,
                    pointHoverRadius: isBar ? 0 : 6,
                    pointBackgroundColor: powerBIBlue,
                    pointBorderColor: '#ffffff',
                    pointBorderWidth: 2,
                    pointHoverBorderWidth: 3,
                    fill: isLine,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: isBar ? 'y' : 'x',
                animation: {
                    duration: 1200,
                    easing: 'easeOutQuart',
                    delay: (context) => {
                        let delay = 0;
                        if (context.type === 'data' && context.mode === 'default') {
                            delay = context.dataIndex * 50; // Stagger each bar by 50ms
                        }
                        return delay;
                    },
                    onProgress: function(animation) {
                        // Smooth progress tracking
                        if (animation.currentStep === 1) {
                            animation.chart.canvas.style.opacity = '1';
                        }
                    }
                },
                transitions: {
                    active: {
                        animation: {
                            duration: 400
                        }
                    }
                },
                layout: {
                    padding: {
                        left: isBar ? 8 : 5,
                        right: 15,
                        top: 10,
                        bottom: 5
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        enabled: true,
                        backgroundColor: 'rgba(0, 0, 0, 0.85)',
                        titleColor: '#FFFFFF',
                        titleFont: {
                            size: 13,
                            weight: '600',
                            family: "'Segoe UI', sans-serif"
                        },
                        bodyColor: '#E8E8E8',
                        bodyFont: {
                            size: 12,
                            family: "'Segoe UI', sans-serif"
                        },
                        padding: 12,
                        borderColor: 'rgba(255, 255, 255, 0.1)',
                        borderWidth: 1,
                        cornerRadius: 6,
                        displayColors: true,
                        boxWidth: 12,
                        boxHeight: 12,
                        mode: 'index',
                        intersect: isBar,
                        callbacks: isBar ? {
                            label: (ctx) => `${metricLabel}: ${Number(ctx.raw).toLocaleString()}`
                        } : {
                            label: (ctx) => `${metricLabel}: ${Number(ctx.raw).toLocaleString()}`
                        }
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
                        grid: { 
                            color: isBar ? 'rgba(0, 0, 0, 0.08)' : 'rgba(0, 0, 0, 0.05)', 
                            drawBorder: false,
                            lineWidth: 1
                        },
                        ticks: { 
                            font: { 
                                size: 11, 
                                family: "'Segoe UI', sans-serif",
                                weight: '500'
                            }, 
                            color: '#605E5C',
                            padding: 8,
                            callback: isBar
                                ? (value) => Number(value).toLocaleString()
                                : undefined
                        },
                        title: isBar ? { 
                            display: true, 
                            text: metricLabel, 
                            color: '#605E5C', 
                            font: { 
                                size: 12,
                                weight: '600',
                                family: "'Segoe UI', sans-serif"
                            },
                            padding: 10
                        } : undefined
                    },
                    y: {
                        beginAtZero: !isBar,
                        grid: { 
                            color: isBar ? 'transparent' : 'rgba(0, 0, 0, 0.08)', 
                            drawBorder: false,
                            lineWidth: 1
                        },
                        ticks: { 
                            font: { 
                                size: 11,
                                family: "'Segoe UI', sans-serif",
                                weight: '500'
                            }, 
                            color: '#605E5C',
                            padding: 8,
                            autoSkip: false,
                            callback: isBar
                                ? function (value) {
                                    const label = this.getLabelForValue(value);
                                    if (!label) return '';
                                    return label.length > 22 ? `${label.slice(0, 20)}…` : label;
                                }
                                : (value) => Number(value).toLocaleString()
                        }
                    }
                }
            }
        };
    }

    const chart = new Chart(canvas, chartConfig);
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

    // Show different loading text for DeepSeek (reasoning models)
    const model = modelSelect && modelSelect.value ? modelSelect.value : '';
    const isDeepSeek = model.toLowerCase().includes('deepseek');

    const loadingText = document.createElement('span');
    loadingText.textContent = isDeepSeek ? '💭 Thinking & writing SQL' : 'Writing SQL';

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
            <div class="debug-section-title">${debug.engine === 'text_to_sql' ? 'Text-to-SQL' : 'Query plan'}</div>
            <div class="debug-badge ${debug.chart_type ? 'success' : 'info'}">
                ${debug.model || debug.engine || debug.chart_type || plan?.metric || 'N/A'}
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
    } else if (debug.sql_raw) {
        const isError = debug.sql_raw.startsWith('ERROR:');
        html += `
            <div class="debug-section">
                <div class="debug-section-title">${isError ? 'LLM call failed' : 'Raw model output (no SQL extracted)'}</div>
                <div class="debug-badge ${isError ? 'error' : 'info'}">${isError ? 'error' : 'unparsed'}</div>
                <div class="debug-code">${escapeHtml(debug.sql_raw)}</div>
            </div>
        `;
    }

    if (debug.sql_guard) {
        html += `
            <div class="debug-section">
                <div class="debug-section-title">SQL safety check</div>
                <div class="debug-metric">
                    <span class="debug-metric-label">Result</span>
                    <span class="debug-metric-value">${debug.sql_guard.ok ? 'allowed' : 'rejected'}</span>
                </div>
                <div class="debug-code">${escapeHtml(debug.sql_guard.reason || '')}</div>
            </div>
        `;
    }

    if (debug.sql_retries && debug.sql_retries.length) {
        html += `
            <div class="debug-section">
                <div class="debug-section-title">SQL self-correct</div>
                <div class="debug-metric">
                    <span class="debug-metric-label">Failed attempts</span>
                    <span class="debug-metric-value">${debug.sql_retries.length}</span>
                </div>
                <div class="debug-code">${escapeHtml(debug.sql_retries.map((a) => a.error).join('\n\n'))}</div>
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

async function loadChatModels() {
    if (!modelSelect) return;
    try {
        const response = await fetch(`${API_BASE_URL}/api/models`);
        if (!response.ok) throw new Error(String(response.status));
        const payload = await response.json();
        const models = payload.models || [];
        const saved = localStorage.getItem(MODEL_STORAGE_KEY);
        const fallback = (models.find((m) => m.default) || models[0] || {}).id;
        const selected = models.some((m) => m.id === saved && m.ready) ? saved : fallback;
        
        modelSelect.innerHTML = '';
        
        // Group models by provider
        const ollama = models.filter(m => m.provider === 'ollama');
        const cloud = models.filter(m => m.provider !== 'ollama');
        
        // Add Ollama models
        if (ollama.length > 0) {
            const group1 = document.createElement('optgroup');
            group1.label = 'Local Models (Ollama)';
            ollama.forEach((m) => {
                const opt = document.createElement('option');
                opt.value = m.id;
                if (m.ready) {
                    opt.textContent = `${m.label} — ${m.note}`;
                } else {
                    opt.textContent = `${m.label} — download required`;
                    opt.disabled = true;
                }
                group1.appendChild(opt);
            });
            modelSelect.appendChild(group1);
        }
        
        // Add cloud models
        if (cloud.length > 0) {
            const group2 = document.createElement('optgroup');
            group2.label = 'Cloud Models (API Key Required)';
            cloud.forEach((m) => {
                const opt = document.createElement('option');
                opt.value = m.id;
                if (m.ready) {
                    opt.textContent = `${m.label} — ${m.note}`;
                } else {
                    const provider = m.provider === 'openai' ? 'OpenAI' : 'Anthropic';
                    opt.textContent = `${m.label} — ${provider} API key required`;
                    opt.disabled = true;
                }
                group2.appendChild(opt);
            });
            modelSelect.appendChild(group2);
        }
        
        if (selected) modelSelect.value = selected;
        
        // Retry if any Ollama models are pending download
        const pending = ollama.some((m) => !m.ready);
        if (pending) setTimeout(loadChatModels, 8000);
    } catch (error) {
        console.warn('Could not load models:', error.message);
    }
}

if (modelSelect) {
    modelSelect.addEventListener('change', () => {
        localStorage.setItem(MODEL_STORAGE_KEY, modelSelect.value);
    });
}

checkApiHealth();
loadChatModels();
