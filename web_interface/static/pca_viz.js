const USE_FIXED_AXIS_RANGE = true; // Switch to control axis range behavior

let pcaData = {
    activeStudy: null,
    metadata: null,
    filters: {},
    plotData: null
};

// Initialize
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('pca_viz')) {
        loadPcaStudies();
    }
});

async function loadPcaStudies() {
    const selector = document.getElementById('pca-study-select');
    try {
        // Reuse the generic studies endpoint
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
            // Auto select first? No, let user choose.
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
    loadPcaMetadata();
}

async function loadPcaMetadata() {
    document.getElementById('pca-status').innerText = "Loading metadata...";

    try {
        const res = await fetch(`/api/pca/metadata`, {
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

        // Initial Plot
        updatePcaPlot();

    } catch (e) {
        console.error(e);
        document.getElementById('pca-status').innerText = "Error loading metadata";
    }
}

function renderPcaControls(data) {
    const xSelect = document.getElementById('pca-x-select');
    const ySelect = document.getElementById('pca-y-select');
    const colorSelect = document.getElementById('pca-color-select');

    xSelect.innerHTML = '';
    ySelect.innerHTML = '';
    colorSelect.innerHTML = '';

    // X/Y Axis: Numeric Columns (Components + Outcomes)
    data.numeric_cols.forEach(col => {
        const optX = document.createElement('option');
        optX.value = col;
        optX.text = col;
        xSelect.appendChild(optX);

        const optY = document.createElement('option');
        optY.value = col;
        optY.text = col;
        ySelect.appendChild(optY);
    });

    // Defaults: C0 and C1 if available
    if (data.numeric_cols.includes('C0')) xSelect.value = 'C0';
    if (data.numeric_cols.includes('C1')) ySelect.value = 'C1';

    // Color: Factors (Filterable columns)
    data.factor_cols.forEach(col => {
        const opt = document.createElement('option');
        opt.value = col;
        opt.text = col;
        colorSelect.appendChild(opt);
    });

    // Default color: First factor
    if (data.factor_cols.length > 0) colorSelect.value = data.factor_cols[0];
}

function renderPcaFilters(data) {
    const container = document.getElementById('pca-filters');
    container.innerHTML = '';

    data.factor_cols.forEach(col => {
        // We only support categorical/list filtering for factors in this view
        // assuming factors are categorical.
        // We need values for these factors. Metadata should provide unique values.

        const wrapper = document.createElement('div');
        wrapper.className = 'filter-group';
        wrapper.style.marginBottom = '15px';

        const label = document.createElement('div');
        label.innerText = col;
        label.style.fontWeight = 'bold';
        label.style.marginBottom = '5px';
        wrapper.appendChild(label);

        const values = data.factor_values[col] || [];

        const listDiv = document.createElement('div');
        listDiv.style.maxHeight = '150px';
        listDiv.style.overflowY = 'auto';
        listDiv.style.border = '1px solid #444';
        listDiv.style.padding = '5px';
        listDiv.style.background = '#252526';

        values.forEach(val => {
            const row = document.createElement('div');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = val;
            cb.style.marginRight = '5px';

            // Check if active
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
                updatePcaPlot();
            };

            const span = document.createElement('span');
            span.innerText = val;
            span.style.fontSize = '0.9em';

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
    // Re-render filters to clear checkboxes
    renderPcaFilters(pcaData.metadata);
    updatePcaPlot();
}

async function updatePcaPlot() {
    if (!pcaData.activeStudy) return;

    const xCol = document.getElementById('pca-x-select').value;
    const yCol = document.getElementById('pca-y-select').value;
    const colorCol = document.getElementById('pca-color-select').value;

    if (!xCol || !yCol) return;

    try {
        const res = await fetch('/api/pca/data', {
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

        renderPlotlyChart(data.data, xCol, yCol, colorCol);

    } catch (e) {
        console.error(e);
    }
}

function renderPlotlyChart(dataPoints, xLabel, yLabel, colorLabel) {
    // Color Palette
    const colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
        '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'
    ];

    // Grouping logic
    const groups = {};
    dataPoints.forEach(d => {
        const g = d.color_val || 'Undefined';
        if (!groups[g]) groups[g] = { x: [], y: [], text: [], name: g };
        groups[g].x.push(d.x);
        groups[g].y.push(d.y);
        groups[g].text.push(d.text);
    });

    const groupsKeys = Object.keys(groups);
    let traces = [];

    // Ellipses Logic
    // We render ellipses BEFORE scatter points so points are on top, 
    // OR we rely on opacity. Rendering before is safer for visibility.
    const showEllipses = document.getElementById('pca-show-ellipses')?.checked;

    if (showEllipses) {
        groupsKeys.forEach((g, i) => {
            const groupData = groups[g];
            if (groupData.x.length < 2) return; // Need variance

            const n = groupData.x.length;
            const meanX = groupData.x.reduce((a, b) => a + b, 0) / n;
            const meanY = groupData.y.reduce((a, b) => a + b, 0) / n;

            // Variance (Sample)
            const varX = groupData.x.reduce((a, b) => a + Math.pow(b - meanX, 2), 0) / (n - 1);
            const varY = groupData.y.reduce((a, b) => a + Math.pow(b - meanY, 2), 0) / (n - 1);

            // Radii = Standard Deviation (sqrt of variance)
            // Scaling? User said "size determined by variance". Usually this visually means SD.
            // Let's use 2 * SD to cover ~95% if normal, which looks like a "cluster".
            // Or just 1 SD. User asked for "size determined by variance". 
            // sqrt(var) = std dev.
            const rx = Math.sqrt(varX) * 2;
            const ry = Math.sqrt(varY) * 2;

            // Generate Ellipse Points
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
                showlegend: false, // Don't clutter legend? Or maybe logical to hide.
                line: { width: 0 }, // No border line? or thin?
                fill: 'toself',
                fillcolor: color,
                opacity: 0.2, // High transparency
                hoverinfo: 'skip'
            });
        });
    }

    // Scatter Traces
    groupsKeys.forEach((g, i) => {
        const color = colors[i % colors.length];
        traces.push({
            x: groups[g].x,
            y: groups[g].y,
            mode: 'markers',
            type: 'scatter',
            name: g,
            text: groups[g].text,
            hoverinfo: 'text',
            marker: {
                size: 8,
                opacity: 0.8,
                color: color
            }
        });
    });

    // Axis Range Configuration
    const axisConfig = {
        title: { font: { size: 14, color: '#ccc' } },
        gridcolor: '#444',
        zerolinecolor: '#888'
    };

    if (USE_FIXED_AXIS_RANGE) {
        axisConfig.range = [-4, 4];
    }

    // Build Layout
    const layout = {
        title: `${xLabel} vs ${yLabel} (Color: ${colorLabel})`,
        xaxis: { title: xLabel, ...axisConfig },
        yaxis: { title: yLabel, ...axisConfig },
        hovermode: 'closest',
        paper_bgcolor: '#1e1e1e',
        plot_bgcolor: '#1e1e1e',
        font: { color: '#ccc' },
        annotations: []
    };

    // Add Interpretation Annotations
    if (pcaData.metadata && pcaData.metadata.interpretations) {
        const inter = pcaData.metadata.interpretations;

        // Helper to format long text to multi-line (approx 30 chars per line)
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
                xref: isX ? 'paper' : 'paper',
                yref: isX ? 'paper' : 'paper',
                text: formatLabel(text),
                showarrow: false,
                font: { size: 10, color: '#aaa' },
                bgcolor: '#252526',
                bordercolor: '#444',
                borderwidth: 1,
                opacity: 0.8
            };

            if (isX) {
                if (direction === 'pos') {
                    ann.x = 1;
                    ann.y = 0.5;
                    ann.xanchor = 'right';
                    ann.yanchor = 'middle';
                    ann.xshift = -10;
                } else {
                    ann.x = 0;
                    ann.y = 0.5;
                    ann.xanchor = 'left';
                    ann.yanchor = 'middle';
                    ann.xshift = 10;
                }
            } else {
                if (direction === 'pos') {
                    ann.x = 0.5;
                    ann.y = 1;
                    ann.xanchor = 'center';
                    ann.yanchor = 'top';
                    ann.yshift = -10;
                } else {
                    ann.x = 0.5;
                    ann.y = 0;
                    ann.xanchor = 'center';
                    ann.yanchor = 'bottom';
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

    // Regression Logic
    const showStats = document.getElementById('pca-show-stats')?.checked;
    if (showStats && dataPoints.length > 1) {
        const reg = calculateRegression(dataPoints);
        if (reg) {
            // Regression Line Trace
            // Need range of X values in data to draw line
            const xVals = dataPoints.map(d => d.x);
            const minX = Math.min(...xVals);
            const maxX = Math.max(...xVals);

            // To make the line extend to the edges of the fixed axis range (if active),
            // we could evaluate at -4 and 4.
            // But usually safer to draw within data range or slightly beyond.
            // If fixed axis, let's draw across the visible area [-4, 4] for visual continuity.
            const lineX = USE_FIXED_AXIS_RANGE ? [-4, 4] : [minX, maxX];
            const lineY = lineX.map(x => reg.slope * x + reg.intercept);

            const lineTrace = {
                x: lineX,
                y: lineY,
                mode: 'lines',
                type: 'scatter',
                name: 'Regression',
                line: { color: 'rgba(255, 255, 255, 0.5)', width: 2, dash: 'dash' },
                hoverinfo: 'none'
            };
            traces.push(lineTrace);

            // Stats Annotation (Top Left)
            // Position: x=0 (left), y=1 (top) in paper coordinates
            const sign = reg.intercept >= 0 ? '+' : '-';
            const eq = `y = ${reg.slope.toFixed(2)}x ${sign} ${Math.abs(reg.intercept).toFixed(2)}`;
            const r2Text = `R² = ${reg.r2.toFixed(2)}`;

            layout.annotations.push({
                xref: 'paper',
                yref: 'paper',
                x: 0.02,
                y: 0.98,
                xanchor: 'left',
                yanchor: 'top',
                text: `${r2Text}`,
                showarrow: false,
                font: { size: 12, color: '#fff' },
                bgcolor: 'rgba(0,0,0,0.7)',
                bordercolor: '#666',
                borderwidth: 1,
                align: 'left'
            });
        }
    }

    Plotly.newPlot('pca-plot', traces, layout);
}

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
    if (denominator === 0) return null; // Vertical line or single point

    const slope = (n * sumXY - sumX * sumY) / denominator;
    const intercept = (sumY - slope * sumX) / n;

    // R-squared
    const meanY = sumY / n;
    let ssTot = 0, ssRes = 0;
    for (let i = 0; i < n; i++) {
        const y = data[i].y;
        const yPred = slope * data[i].x + intercept;
        ssTot += (y - meanY) * (y - meanY);
        ssRes += (y - yPred) * (y - yPred);
    }

    // Avoid division by zero if all Y are same
    const r2 = ssTot === 0 ? 0 : 1 - (ssRes / ssTot);

    return { slope, intercept, r2 };
}
