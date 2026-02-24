// Chart.js interop helpers for Azure MACC Analyst
window.ChartInterop = {
    _instances: {},

    renderBarChart: function (canvasId, labels, data, label, color) {
        // Destroy existing instance if any
        if (this._instances[canvasId]) {
            this._instances[canvasId].destroy();
            delete this._instances[canvasId];
        }

        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        this._instances[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: label || 'Cost',
                    data: data,
                    backgroundColor: color || '#0078D4',
                    borderColor: color || '#0078D4',
                    borderWidth: 1,
                    borderRadius: 3,
                    barPercentage: 0.7,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return '$' + ctx.parsed.y.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: {
                            callback: function (value) {
                                if (value >= 1000000) return '$' + (value / 1000000).toFixed(1) + 'M';
                                if (value >= 1000) return '$' + (value / 1000).toFixed(1) + 'K';
                                return '$' + value.toFixed(2);
                            }
                        },
                        grid: { color: '#eee' }
                    },
                    x: {
                        ticks: {
                            maxRotation: 45,
                            font: { size: 11 }
                        },
                        grid: { display: false }
                    }
                }
            }
        });
    },

    renderLineChart: function (canvasId, labels, data, label, color) {
        if (this._instances[canvasId]) {
            this._instances[canvasId].destroy();
            delete this._instances[canvasId];
        }

        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        this._instances[canvasId] = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: label || 'Cost',
                    data: data,
                    borderColor: color || '#0078D4',
                    backgroundColor: (color || '#0078D4') + '20',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 3,
                    pointHoverRadius: 6,
                    borderWidth: 2,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function (ctx) {
                                return '$' + ctx.parsed.y.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: {
                            callback: function (value) {
                                if (value >= 1000000) return '$' + (value / 1000000).toFixed(1) + 'M';
                                if (value >= 1000) return '$' + (value / 1000).toFixed(1) + 'K';
                                return '$' + value.toFixed(2);
                            }
                        },
                        grid: { color: '#eee' }
                    },
                    x: {
                        grid: { display: false }
                    }
                }
            }
        });
    },

    destroyChart: function (canvasId) {
        if (this._instances[canvasId]) {
            this._instances[canvasId].destroy();
            delete this._instances[canvasId];
        }
    }
};
