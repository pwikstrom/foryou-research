const USE_FIXED_AXIS_RANGE = true;

let pcaData = {
    activeStudy: null,
    metadata: null,
    filters: {},
    plotData: null,
    currentView: 'scatter'      // 'scatter' or 'heatmap'
};

let _lastScatterArgs = null;
let _lastHeatmapArgs = null;
let _correlationsFilterVisible = true;


window.correlationsToggleSidebar = function () {
    _correlationsFilterVisible = !_correlationsFilterVisible;
    const panel = document.getElementById('correlations-filter-panel');
    if (panel) panel.style.display = _correlationsFilterVisible ? 'flex' : 'none';
    requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
};


// Initialize
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('correlations')) {
        loadPcaStudies();
    }
});


async function loadPcaStudies() {
    const selector = document.getElementById('pca-study-select');
    try {
        const res = await fetch('/api/studies/defined');
        const studies = await res.json();

        if (studies.length > 0) {
            selector.innerHTML = '<option value="" disabled selected>Select a study...</option>';
            studies.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s;
                opt.text = s;
                selector.appendChild(opt);
            });

            // Auto-load the first accessible study if no study is currently active
            if (!pcaData.activeStudy && studies.length > 0) {
                selector.value = studies[0];
                changePcaStudy();
            }
        } else {
            selector.innerHTML = '<option disabled>No studies found</option>';
        }
    } catch (e) {
        console.error(e);
        selector.innerHTML = '<option disabled>Error loading studies</option>';
    }
}


function changePcaStudy() {
    const selector = document.getElementById('pca-study-select');
    const study = selector.value;
    if (!study) return;

    pcaData.activeStudy = study;
    pcaData.filters = {};

    // Clear the existing plot so stale data isn't displayed on error
    const plotDiv = document.getElementById('pca-plot');
    if (plotDiv) {
        Plotly.purge(plotDiv);
    }

    loadPcaMetadata();
}


async function loadPcaMetadata() {
    document.getElementById('pca-status').innerText = "Loading metadata...";

    try {
        const res = await fetch(`/api/correlations/metadata`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ study: pcaData.activeStudy })
        });
        const data = await res.json();

        if (data.error) {
            document.getElementById('pca-status').innerText = `Error: ${data.error}`;
            return;
        }

        pcaData.metadata = data;
        document.getElementById('pca-status').innerText = "Ready";

        renderPcaControls(data);
        renderPcaFilters(data);

        // Initial render based on current view (also applies active button styles)
        setPcaView(pcaData.currentView);

    } catch (e) {
        console.error(e);
        document.getElementById('pca-status').innerText = "Error loading metadata";
    }
}


function renderPcaControls(data) {
    const xSelect = document.getElementById('pca-x-select');
    const ySelect = document.getElementById('pca-y-select');
    const colorSelect = document.getElementById('pca-color-select');

    // Preserve previously selected axes if the user had chosen them and they exist in the new study
    const prevX = xSelect.value;
    const prevY = ySelect.value;

    xSelect.innerHTML = '';
    ySelect.innerHTML = '';
    colorSelect.innerHTML = '';

    // Build display labels with explained variance for components
    const inter = data.interpretations || {};

    // X/Y Axis: Numeric Columns with variance info
    const schemaMap = data.schema_map || {};

    data.numeric_cols.forEach(col => {
        const variance = inter[col]?.explained_variance_pct;
        const displayName = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        const label = variance ? `${displayName} (${variance}%)` : displayName;

        const optX = document.createElement('option');
        optX.value = col;
        optX.text = label;
        xSelect.appendChild(optX);

        const optY = document.createElement('option');
        optY.value = col;
        optY.text = label;
        ySelect.appendChild(optY);
    });

    // Check if the previous selections are still valid options in this new study
    const hasPrevX = data.numeric_cols.includes(prevX);
    const hasPrevY = data.numeric_cols.includes(prevY);

    if (hasPrevX && hasPrevY) {
        // Carry over the existing user selections
        xSelect.value = prevX;
        ySelect.value = prevY;
    } else {
        // Otherwise, pick two random different components
        if (data.numeric_cols.length > 0) {
            let idx1 = Math.floor(Math.random() * data.numeric_cols.length);
            xSelect.value = data.numeric_cols[idx1];

            if (data.numeric_cols.length > 1) {
                let idx2 = idx1;
                while (idx2 === idx1) {
                    idx2 = Math.floor(Math.random() * data.numeric_cols.length);
                }
                ySelect.value = data.numeric_cols[idx2];
            }
        }
    }

    // Colour: Factors — use display_name from schema_map if available
    data.factor_cols.forEach(col => {
        const opt = document.createElement('option');
        opt.value = col;
        opt.text = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        colorSelect.appendChild(opt);
    });

    if (data.factor_cols.length > 0) colorSelect.value = data.factor_cols[0];
}


function renderPcaFilters(data) {
    const container = document.getElementById('pca-filters');
    container.innerHTML = '';

    const schemaMap = data.schema_map || {};
    const displayIds = data.display_ids || {};

    data.factor_cols.forEach(col => {
        const wrapper = document.createElement('div');
        wrapper.className = 'filter-group';
        wrapper.style.marginBottom = '15px';

        const label = document.createElement('div');
        // Use display_name from schema_map if available
        label.innerText = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        label.classList.add('font-bold');
        label.style.marginBottom = '5px';
        wrapper.appendChild(label);

        const values = data.factor_values[col] || [];

        const listDiv = document.createElement('div');
        listDiv.style.maxHeight = '150px';
        listDiv.style.overflowY = 'auto';
        listDiv.style.border = '1px solid var(--chart-grid)';
        listDiv.style.padding = '5px';
        listDiv.style.background = 'var(--color-bg-surface)';

        values.forEach(val => {
            const row = document.createElement('div');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = val;
            cb.style.marginRight = '5px';

            if (pcaData.filters[col] && pcaData.filters[col].includes(val)) {
                cb.checked = true;
            }

            cb.onchange = () => {
                const checked = Array.from(listDiv.querySelectorAll('input:checked')).map(c => c.value);
                if (checked.length > 0) {
                    pcaData.filters[col] = checked;
                } else {
                    delete pcaData.filters[col];
                }
                // Refresh the current view
                if (pcaData.currentView === 'scatter') {
                    updatePcaPlot();
                } else {
                    loadCorrelationHeatmap();
                }
            };

            const span = document.createElement('span');
            // For collection_id, show display_collection_id if available
            if (col === 'collection_id' && displayIds[val]) {
                span.innerText = displayIds[val];
            } else {
                span.innerText = val;
            }
            span.classList.add('text-sm');

            row.appendChild(cb);
            row.appendChild(span);
            listDiv.appendChild(row);
        });

        wrapper.appendChild(listDiv);
        container.appendChild(wrapper);
    });
}


function resetPcaFilters() {
    pcaData.filters = {};
    renderPcaFilters(pcaData.metadata);
    if (pcaData.currentView === 'scatter') {
        updatePcaPlot();
    } else {
        loadCorrelationHeatmap();
    }
}


// --- View Toggle ---

function setPcaView(view) {
    pcaData.currentView = view;

    // Update button styles to highlight active selection
    const scatterBtn = document.getElementById('pca-view-scatter');
    const heatmapBtn = document.getElementById('pca-view-heatmap');
    const scatterControls = document.getElementById('pca-scatter-controls');

    scatterBtn.classList.toggle('active', view === 'scatter');
    heatmapBtn.classList.toggle('active', view !== 'scatter');
    if (view === 'scatter') {
        scatterControls.style.display = 'flex';
        updatePcaPlot();
    } else {
        scatterControls.style.display = 'none';
        loadCorrelationHeatmap();
    }
}


// --- Scatter Plot ---

async function updatePcaPlot() {
    if (!pcaData.activeStudy) return;

    const xCol = document.getElementById('pca-x-select').value;
    const yCol = document.getElementById('pca-y-select').value;
    const colorCol = document.getElementById('pca-color-select').value;

    if (!xCol || !yCol) return;

    try {
        const res = await fetch('/api/correlations/data', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: pcaData.activeStudy,
                filters: pcaData.filters,
                x_col: xCol,
                y_col: yCol,
                color_col: colorCol
            })
        });
        const data = await res.json();

        if (data.error) {
            console.error(data.error);
            return;
        }

        // Update point count
        const countEl = document.getElementById('pca-point-count');
        if (countEl) {
            const shown = data.data.length;
            const total = data.total_count || shown;
            countEl.innerText = shown < total
                ? `Showing ${shown.toLocaleString()} / ${total.toLocaleString()} points`
                : `${total.toLocaleString()} points`;
        }

        renderPlotlyChart(data.data, xCol, yCol, colorCol);

    } catch (e) {
        console.error(e);
    }
}


function renderPlotlyChart(dataPoints, xLabel, yLabel, colorLabel) {
    _lastScatterArgs = { dataPoints, xLabel, yLabel, colorLabel };
    const colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
        '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'
    ];

    const displayIds = pcaData.metadata?.display_ids || {};

    // Group by color value
    const groups = {};
    dataPoints.forEach(d => {
        const rawColorVal = d.color_val || 'Undefined';
        // Map collection_id to display names for the legend
        const gName = (colorLabel === 'collection_id' && displayIds[rawColorVal]) ? displayIds[rawColorVal] : rawColorVal;

        if (!groups[gName]) groups[gName] = { x: [], y: [], text: [], factors: [], name: gName };
        groups[gName].x.push(d.x);
        groups[gName].y.push(d.y);
        groups[gName].text.push(d.text);
        groups[gName].factors.push(d.factors || {});
    });

    const groupsKeys = Object.keys(groups);
    let traces = [];

    // Ellipses
    const showEllipses = document.getElementById('pca-show-ellipses')?.checked;

    if (showEllipses) {
        groupsKeys.forEach((g, i) => {
            const groupData = groups[g];
            if (groupData.x.length < 2) return;

            const n = groupData.x.length;
            const meanX = groupData.x.reduce((a, b) => a + b, 0) / n;
            const meanY = groupData.y.reduce((a, b) => a + b, 0) / n;

            const varX = groupData.x.reduce((a, b) => a + Math.pow(b - meanX, 2), 0) / (n - 1);
            const varY = groupData.y.reduce((a, b) => a + Math.pow(b - meanY, 2), 0) / (n - 1);

            const rx = Math.sqrt(varX) * 2;
            const ry = Math.sqrt(varY) * 2;

            const numPoints = 50;
            let ellipseX = [];
            let ellipseY = [];
            for (let j = 0; j <= numPoints; j++) {
                const theta = (j / numPoints) * 2 * Math.PI;
                ellipseX.push(meanX + rx * Math.cos(theta));
                ellipseY.push(meanY + ry * Math.sin(theta));
            }

            const color = colors[i % colors.length];

            traces.push({
                x: ellipseX,
                y: ellipseY,
                mode: 'lines',
                name: `${g} (Ellipse)`,
                showlegend: false,
                line: { width: 0 },
                fill: 'toself',
                fillcolor: color,
                opacity: 0.2,
                hoverinfo: 'skip'
            });
        });
    }

    // Scatter traces
    groupsKeys.forEach((g, i) => {
        const color = colors[i % colors.length];
        traces.push({
            x: groups[g].x,
            y: groups[g].y,
            mode: 'markers',
            type: 'scatter',
            name: g,
            text: groups[g].text,
            customdata: groups[g].factors,
            hoverinfo: 'text',
            marker: {
                size: (window.userSettings && window.userSettings.big_dots) ? 16 : 8,
                opacity: (window.userSettings && window.userSettings.big_dots) ? 0.45 : 0.8,
                color: color
            }
        });
    });

    // Build axis labels with variance info
    const inter = pcaData.metadata?.interpretations || {};
    const schemaMap = pcaData.metadata?.schema_map || {};

    const xDisplayName = (schemaMap[xLabel] && schemaMap[xLabel].display_name) ? schemaMap[xLabel].display_name : xLabel;
    const yDisplayName = (schemaMap[yLabel] && schemaMap[yLabel].display_name) ? schemaMap[yLabel].display_name : yLabel;

    const xVariance = inter[xLabel]?.explained_variance_pct;
    const yVariance = inter[yLabel]?.explained_variance_pct;
    const xTitle = xVariance ? `${xDisplayName} (${xVariance}% var.)` : xDisplayName;
    const yTitle = yVariance ? `${yDisplayName} (${yVariance}% var.)` : yDisplayName;

    // Axis Configuration
    const axisConfig = {
        title: { text: '' }, // We use annotations for titles instead
        gridcolor: getCSSVar('--chart-grid'),
        zerolinecolor: getCSSVar('--chart-zeroline')
    };

    if (USE_FIXED_AXIS_RANGE) {
        axisConfig.range = [-4, 4];
    }

    const layout = {
        xaxis: { ...axisConfig },
        yaxis: { ...axisConfig },
        hovermode: 'closest',
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
        annotations: [
            // X-Axis Title Annotation (Anchored at data coordinate 2,0)
            {
                xref: 'x',
                yref: 'y',
                x: 2.0,
                y: 0.0,
                yshift: -15, // Push slightly below the line
                xanchor: 'center',
                yanchor: 'top',
                text: xTitle,
                showarrow: false,
                font: { family: getCSSVar('--font-sans'), size: 16, color: getCSSVar('--chart-text') }
            },
            // Y-Axis Title Annotation (Anchored at data coordinate 0,2)
            {
                xref: 'x',
                yref: 'y',
                x: 0.0,
                y: 2.0,
                xshift: -15, // Push slightly left of the line
                xanchor: 'right',
                yanchor: 'center',
                text: yTitle,
                showarrow: false,
                font: { family: getCSSVar('--font-sans'), size: 16, color: getCSSVar('--chart-text') },
                textangle: -90
            }
        ],
        margin: { t: 60, r: 40, b: 70, l: 70 }
    };

    // Interpretation annotations on axes
    if (inter) {
        const formatLabel = (txt) => {
            if (!txt) return '';
            const maxLen = 30;
            const words = txt.split(' ');
            let lines = [];
            let currentLine = words[0];
            for (let i = 1; i < words.length; i++) {
                if (currentLine.length + 1 + words[i].length <= maxLen) {
                    currentLine += ' ' + words[i];
                } else {
                    lines.push(currentLine);
                    currentLine = words[i];
                }
            }
            lines.push(currentLine);
            return lines.join('<br>');
        };

        const addLabel = (axis, direction, text) => {
            if (!text) return;
            const isX = axis === 'x';

            let ann = {
                xref: 'paper',
                yref: 'paper',
                text: formatLabel(text),
                showarrow: false,
                font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--color-text-tertiary') },
                bgcolor: getCSSVar('--color-bg-surface'),
                bordercolor: getCSSVar('--chart-grid'),
                borderwidth: 1,
                opacity: 0.8
            };

            if (isX) {
                if (direction === 'pos') {
                    ann.x = 1; ann.y = 0.5;
                    ann.xanchor = 'right'; ann.yanchor = 'middle';
                    ann.xshift = -10;
                } else {
                    ann.x = 0; ann.y = 0.5;
                    ann.xanchor = 'left'; ann.yanchor = 'middle';
                    ann.xshift = 10;
                }
            } else {
                if (direction === 'pos') {
                    ann.x = 0.5; ann.y = 1;
                    ann.xanchor = 'center'; ann.yanchor = 'top';
                    ann.yshift = -10;
                } else {
                    ann.x = 0.5; ann.y = 0;
                    ann.xanchor = 'center'; ann.yanchor = 'bottom';
                    ann.yshift = 10;
                }
            }
            layout.annotations.push(ann);
        };

        if (inter[xLabel]) {
            addLabel('x', 'pos', inter[xLabel].top_positive);
            addLabel('x', 'neg', inter[xLabel].top_negative);
        }
        if (inter[yLabel]) {
            addLabel('y', 'pos', inter[yLabel].top_positive);
            addLabel('y', 'neg', inter[yLabel].top_negative);
        }
    }

    // Regression line
    const showStats = document.getElementById('pca-show-stats')?.checked;
    if (showStats && dataPoints.length > 1) {
        const reg = calculateRegression(dataPoints);
        if (reg) {
            const lineX = USE_FIXED_AXIS_RANGE ? [-4, 4] : [Math.min(...dataPoints.map(d => d.x)), Math.max(...dataPoints.map(d => d.x))];
            const lineY = lineX.map(x => reg.slope * x + reg.intercept);

            traces.push({
                x: lineX,
                y: lineY,
                mode: 'lines',
                type: 'scatter',
                name: 'Regression',
                line: { color: getCSSVar('--chart-regression-line'), width: 2, dash: 'dash' },
                hoverinfo: 'none'
            });

            const sign = reg.intercept >= 0 ? '+' : '-';
            layout.annotations.push({
                xref: 'paper', yref: 'paper',
                x: 0.02, y: 0.98,
                xanchor: 'left', yanchor: 'top',
                text: `R² = ${reg.r2.toFixed(2)}`,
                showarrow: false,
                font: { family: getCSSVar('--font-sans'), size: 12, color: getCSSVar('--white') },
                bgcolor: getCSSVar('--chart-badge-bg'),
                bordercolor: getCSSVar('--color-text-faint'), borderwidth: 1,
                align: 'left'
            });
        }
    }

    Plotly.newPlot('pca-plot', traces, layout, { responsive: true, displayModeBar: true });

    // Drill-down: click a dot to view matching videos
    document.getElementById('pca-plot').on('plotly_click', function(eventData) {
        const point = eventData.points[0];
        if (!point || !point.customdata) return;
        const factors = point.customdata;
        if (!factors || Object.keys(factors).length === 0) return;
        drillDownFromCorrelations(factors);
    });
}


function drillDownFromCorrelations(factors) {
    const schemaMap = pcaData.metadata?.schema_map || {};

    // Build filter set from factor values
    const filters = {};
    Object.entries(factors).forEach(([col, val]) => {
        filters[col] = { type: 'category', value: [val] };
    });

    // Build a readable label for the confirmation popup
    const labels = Object.entries(factors).map(([col, val]) => {
        const name = schemaMap[col]?.display_name || col;
        return `${name} = "${val}"`;
    });
    const variableName = 'Correlations';
    const valueLabel = labels.join(', ');

    showDrillDownConfirm(variableName, valueLabel, () => {
        window._pendingDrillDown = {
            study: pcaData.activeStudy,
            filters: filters,
            searchQuery: "",
            timestamp: Date.now()
        };
        const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
        if (tabBtn) tabBtn.click();
    });
}


// --- Correlation Heatmap ---

async function loadCorrelationHeatmap() {
    if (!pcaData.activeStudy) return;

    const countEl = document.getElementById('pca-point-count');
    if (countEl) countEl.innerText = 'Loading heatmap...';

    try {
        const res = await fetch('/api/correlations/correlation_matrix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: pcaData.activeStudy,
                filters: pcaData.filters
            })
        });
        const data = await res.json();

        if (data.error) {
            console.error(data.error);
            if (countEl) countEl.innerText = `Error: ${data.error}`;
            return;
        }

        if (countEl) {
            countEl.innerText = `${data.count.toLocaleString()} observations`;
        }

        renderCorrelationHeatmap(data.columns, data.matrix);

    } catch (e) {
        console.error(e);
        if (countEl) countEl.innerText = 'Error loading heatmap';
    }
}


function renderCorrelationHeatmap(columns, matrix) {
    _lastHeatmapArgs = { columns, matrix };
    const schemaMap = pcaData.metadata?.schema_map || {};

    // Map columns to display names
    const displayColumns = columns.map(col => {
        return (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
    });

    // Build hover text with correlation values
    const hoverText = matrix.map((row, i) =>
        row.map((val, j) => `${displayColumns[i]} × ${displayColumns[j]}<br>r = ${val.toFixed(3)}`)
    );

    const trace = {
        z: matrix,
        x: displayColumns,
        y: displayColumns,
        type: 'heatmap',
        colorscale: [
            [0, '#2166ac'],
            [0.25, '#67a9cf'],
            [0.5, getCSSVar('--chart-heatmap-mid')],
            [0.75, '#ef8a62'],
            [1, '#b2182b']
        ],
        zmin: -1,
        zmax: 1,
        text: hoverText,
        hoverinfo: 'text',
        colorbar: {
            title: { text: 'Pearson r', font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') } },
            tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
            len: 0.8
        }
    };

    const layout = {
        title: {
            text: 'Correlation Matrix',
            font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') }
        },
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text'), size: 10 },
        xaxis: {
            tickangle: -45,
            tickfont: { family: getCSSVar('--font-sans'), size: 9 },
            gridcolor: getCSSVar('--chart-grid')
        },
        yaxis: {
            autorange: 'reversed',
            tickfont: { family: getCSSVar('--font-sans'), size: 9 },
            gridcolor: getCSSVar('--chart-grid')
        },
        margin: { t: 50, r: 80, b: 120, l: 120 }
    };

    Plotly.newPlot('pca-plot', [trace], layout, { responsive: true, displayModeBar: true });
}


// --- Regression Helper ---

function calculateRegression(data) {
    const n = data.length;
    let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0, sumYY = 0;

    for (let i = 0; i < n; i++) {
        const x = data[i].x;
        const y = data[i].y;
        sumX += x;
        sumY += y;
        sumXY += x * y;
        sumXX += x * x;
        sumYY += y * y;
    }

    const denominator = (n * sumXX - sumX * sumX);
    if (denominator === 0) return null;

    const slope = (n * sumXY - sumX * sumY) / denominator;
    const intercept = (sumY - slope * sumX) / n;

    const meanY = sumY / n;
    let ssTot = 0, ssRes = 0;
    for (let i = 0; i < n; i++) {
        const y = data[i].y;
        const yPred = slope * data[i].x + intercept;
        ssTot += (y - meanY) * (y - meanY);
        ssRes += (y - yPred) * (y - yPred);
    }

    const r2 = ssTot === 0 ? 0 : 1 - (ssRes / ssTot);

    return { slope, intercept, r2 };
}

window.addEventListener('theme-changed', () => {
    // Re-render charts (Plotly needs resolved color values, not CSS var())
    if (pcaData.currentView === 'scatter' && _lastScatterArgs) {
        renderPlotlyChart(_lastScatterArgs.dataPoints, _lastScatterArgs.xLabel,
                          _lastScatterArgs.yLabel, _lastScatterArgs.colorLabel);
    } else if (pcaData.currentView === 'heatmap' && _lastHeatmapArgs) {
        renderCorrelationHeatmap(_lastHeatmapArgs.columns, _lastHeatmapArgs.matrix);
    }
});
