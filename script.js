let selectedMetric = "cpu";
let currentWindow = 60;
let showPowerSeries = true;
let mainChart = null;
let donutChart = null;
let fetchInProgress = false;
let pollHandle = null;
let latestData = null;
let alertHistoryQueue = [];
const ALERT_HISTORY_LIMIT = 10;

function byId(id) {
  return document.getElementById(id);
}

function metricColor(metric) {
  return ({
    cpu: "#1f85ff",
    gpu: "#7c6cff",
    memory_disk: "#7ad7ff",
    power: "#f7bf54",
    energy_total: "#b386f8",
    carbon_rate: "#48d6b2",
  })[metric] || "#1f85ff";
}

function metricLabel(metric) {
  return ({
    cpu: "CPU Usage",
    gpu: "GPU Usage",
    memory_disk: "RAM / Disk Usage",
    power: "Power Consumption",
    energy_total: "Energy Consumed",
    carbon_rate: "Carbon Emission Rate",
  })[metric] || "CPU Usage";
}

function metricUnit(metric) {
  return ({
    cpu: "%",
    gpu: "%",
    memory_disk: "%",
    power: "W",
    energy_total: "Wh",
    carbon_rate: "kg/hr",
  })[metric] || "";
}

function setText(id, value) {
  const el = byId(id);
  if (el) el.innerText = value;
}

function setTooltipContent(id, text) {
  const el = byId(id);
  if (el) el.setAttribute("data-tip", text || "");
}

function initCharts() {
  const mainCanvas = byId("mainChart");
  const donutCanvas = byId("donutChart");
  if (!mainCanvas || !donutCanvas) return;

  if (!mainChart) {
    mainChart = new Chart(mainCanvas.getContext("2d"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "CPU Usage",
            data: [],
            borderColor: "#1f85ff",
            backgroundColor: "transparent",
            borderWidth: 3,
            tension: 0.28,
            pointRadius: 0,
            pointHoverRadius: 4,
          },
          {
            label: "Disk Usage",
            data: [],
            borderColor: "#7ad7ff",
            backgroundColor: "transparent",
            borderWidth: 2.25,
            tension: 0.28,
            pointRadius: 0,
            pointHoverRadius: 4,
            hidden: true,
          },
          {
            label: "Power Consumption",
            data: [],
            borderColor: "#f7bf54",
            backgroundColor: "transparent",
            borderWidth: 2.75,
            tension: 0.28,
            pointRadius: 0,
            pointHoverRadius: 4,
          },
          {
            label: "Peak",
            type: "scatter",
            data: [],
            showLine: false,
            pointRadius: 5,
            pointHoverRadius: 6,
            backgroundColor: "#ff5757",
            borderColor: "#ff5757",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        normalized: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            labels: {
              color: getComputedStyle(document.documentElement).getPropertyValue("--legend-text").trim() || "#68758f",
              boxWidth: 24,
              font: { size: 12, weight: "600" },
            },
          },
          tooltip: {
            callbacks: {
              label(context) {
                const value = context.parsed.y ?? context.raw ?? 0;
                const label = context.dataset.label || "Value";
                return `${label}: ${value}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: {
              color: getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#7a879d",
              maxTicksLimit: currentWindow <= 60 ? 7 : 8,
            },
          },
          y: {
            beginAtZero: true,
            grid: { color: "rgba(120,140,170,0.16)" },
            ticks: {
              color: getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#7a879d",
            },
          },
        },
      },
    });
  }

  if (!donutChart) {
    donutChart = new Chart(donutCanvas.getContext("2d"), {
      type: "doughnut",
      data: {
        labels: ["Value", "Remaining"],
        datasets: [
          {
            data: [0, 100],
            backgroundColor: ["#1f85ff", "#e8edf5"],
            borderWidth: 0,
            cutout: "72%",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
      },
    });
  }
}

function updateChartTheme() {
  if (!mainChart) return;
  const root = getComputedStyle(document.documentElement);
  const muted = root.getPropertyValue("--muted").trim() || "#7a879d";
  const legendText = root.getPropertyValue("--legend-text").trim() || muted;
  mainChart.options.plugins.legend.labels.color = legendText;
  mainChart.options.scales.x.ticks.color = muted;
  mainChart.options.scales.y.ticks.color = muted;
  mainChart.update("none");
}

function getPrimarySeries(history) {
  let primaryLabel = "CPU Usage";
  let primaryData = history.cpu || [];
  let primaryColor = metricColor("cpu");
  let secondaryLabel = "Disk Usage";
  let secondaryData = [];
  let secondaryHidden = true;

  if (selectedMetric === "gpu") {
    primaryLabel = "GPU Usage";
    primaryData = history.gpu || [];
    primaryColor = metricColor("gpu");
  } else if (selectedMetric === "memory_disk") {
    primaryLabel = "RAM Usage";
    primaryData = history.ram || [];
    primaryColor = metricColor("memory_disk");
    secondaryLabel = "Disk Usage";
    secondaryData = history.disk || [];
    secondaryHidden = false;
  } else if (selectedMetric === "power") {
    primaryLabel = "Power Consumption";
    primaryData = history.power || [];
    primaryColor = metricColor("power");
  } else if (selectedMetric === "energy_total") {
    primaryLabel = "Energy Consumed";
    primaryData = history.energy_total || [];
    primaryColor = metricColor("energy_total");
  } else if (selectedMetric === "carbon_rate") {
    primaryLabel = "Carbon Emission Rate";
    primaryData = history.carbon_rate || [];
    primaryColor = metricColor("carbon_rate");
  }

  return { primaryLabel, primaryData, primaryColor, secondaryLabel, secondaryData, secondaryHidden };
}

function updateMainChart(data) {
  initCharts();
  if (!mainChart) return;

  const history = data.history || {};
  const labels = history.labels || [];
  const powerData = history.power || [];
  const { primaryLabel, primaryData, primaryColor, secondaryLabel, secondaryData, secondaryHidden } = getPrimarySeries(history);

  let peakVal = 0;
  let peakIndex = -1;
  const seriesForPeak = secondaryHidden
    ? primaryData
    : primaryData.map((v, i) => Math.max(Number(v || 0), Number((secondaryData || [])[i] || 0)));

  if (seriesForPeak.length) {
    peakVal = Math.max(...seriesForPeak);
    peakIndex = seriesForPeak.indexOf(peakVal);
  }

  mainChart.options.scales.x.ticks.maxTicksLimit = currentWindow <= 60 ? 7 : 8;
  mainChart.data.labels = labels;
  mainChart.data.datasets[0].label = primaryLabel;
  mainChart.data.datasets[0].data = primaryData;
  mainChart.data.datasets[0].borderColor = primaryColor;

  mainChart.data.datasets[1].label = secondaryLabel;
  mainChart.data.datasets[1].data = secondaryData;
  mainChart.data.datasets[1].hidden = secondaryHidden;

  mainChart.data.datasets[2].label = "Power Consumption";
  mainChart.data.datasets[2].data = powerData;
  mainChart.data.datasets[2].hidden = !showPowerSeries || selectedMetric === "power";

  mainChart.data.datasets[3].data = peakIndex >= 0 && labels[peakIndex] !== undefined
    ? [{ x: labels[peakIndex], y: peakVal }]
    : [];

  mainChart.update("none");

  setText("peakValue", `${Number(peakVal || 0).toFixed(1)} ${metricUnit(selectedMetric)}`.trim());
  setText("peakTime", peakIndex >= 0 ? labels[peakIndex] : "--:--:--");
}

function updateDonut(value) {
  initCharts();
  if (!donutChart) return;
  let numeric = Number(value || 0);
  if (!["cpu", "gpu", "memory_disk"].includes(selectedMetric)) {
    numeric = Math.min(numeric, 100);
  }
  numeric = Math.max(0, Math.min(100, numeric));
  donutChart.data.datasets[0].data = [numeric, Math.max(0, 100 - numeric)];
  donutChart.update("none");
  setText("donutValue", Number(value || 0).toFixed(selectedMetric === "power" || selectedMetric === "energy_total" || selectedMetric === "carbon_rate" ? 1 : 1).replace(/\.0$/, ""));
  setText("donutUnit", metricUnit(selectedMetric));
}

function updateStaticInfo(data) {
  setText("ownerNameTop", data.owner_name || "User");
  setText("deviceName", data.device_name || "Device");
  setText("deviceModel", data.model || "Model");
  setText("deviceOS", data.os || "OS");
  setText("cpuName", data.cpu_name || "-");
  setText("gpuName", data.gpu_name || "-");

  const tips = data.tooltips || {};
  setTooltipContent("powerInfoDot", tips.power);
  setTooltipContent("carbonInfoDot", tips.co2_rate);
  setTooltipContent("energyInfoDot", tips.energy);
  setTooltipContent("batteryInfoDot", tips.battery);
  setTooltipContent("co2TotalInfoDot", tips.co2_total);
  setTooltipContent("energyRateInfoDot", tips.energy_rate);
  setTooltipContent("greenScoreInfoDot", tips.green_score);
}

function updateCards(data) {
  setText("cpuVal", data.cpu ?? 0);
  setText("gpuVal", data.gpu ?? 0);
  setText("ramVal", data.ram ?? 0);
  setText("diskCardVal", data.disk ?? 0);
  setText("powerVal", data.power ?? 0);
  setText("energyVal", data.energy_total ?? 0);
  setText("carbonVal", data.carbon_rate ?? 0);
  setText("wifiStatus", data.wifi_status || "-");
  setText("ethernetStatus", data.ethernet_status || "-");
  setText("internetTop", `${data.internet ?? 0} KB/s`);
  const hasBattery = !!data.has_battery;
  if (!hasBattery) {
    setText("batteryValTop", "Direct Power");
    setText("batteryStatusTop", data.battery_status || "Line Connected");
    setText("batteryTimeTop", data.battery_time_text || "No battery installed.");
  } else {
    setText("batteryValTop", data.battery === null ? "N/A" : `${data.battery}%`);
    setText("batteryStatusTop", data.battery_status || "-");
    setText("batteryTimeTop", data.battery_time_text || "Estimating...");
  }
  setText("co2TotalVal", `${data.carbon_total ?? 0} kg`);
  setText("co2PredictVal", `1 Min: ${data.predicted_co2_1min ?? 0} kg | 5 Min: ${data.predicted_co2_5min ?? 0} kg`);
  setText("energyRateTop", `${data.energy_rate ?? 0} Wh / sec`);
  setText("futureEnergy", `${data.future_energy ?? 0} Wh / sec`);
  setText("powerStatusOverview", `Status: ${data.power_status || "Good"}`);
  setText("greenScoreVal", `${data.efficiency_score ?? 0}%`);
  setText("greenScoreBadge", data.efficiency_label || "Efficient");
  updateGreenScoreStyle(data.efficiency_class || "good");
  updatePowerModal(data);
}

function updateGreenScoreStyle(scoreClass) {
  const badge = byId("greenScoreBadge");
  const value = byId("greenScoreVal");
  if (!badge || !value) return;
  badge.classList.remove("score-good", "score-moderate", "score-poor");
  value.classList.remove("score-text-good", "score-text-moderate", "score-text-poor");
  if (scoreClass === "poor") {
    badge.classList.add("score-poor");
    value.classList.add("score-text-poor");
  } else if (scoreClass === "moderate") {
    badge.classList.add("score-moderate");
    value.classList.add("score-text-moderate");
  } else {
    badge.classList.add("score-good");
    value.classList.add("score-text-good");
  }
}

function getCurrentTimestamp() {
  const now = new Date();
  return now.toLocaleTimeString([], { hour12: false });
}

function updateAlertHistoryTooltip() {
  const dot = byId("alertHistoryDot");
  if (!dot) return;
  if (!alertHistoryQueue.length) {
    dot.setAttribute("data-tip", "No alerts yet.");
    return;
  }
  const lines = alertHistoryQueue.map((entry, index) => `${index + 1}. [${entry.time}] ${entry.message}`);
  dot.setAttribute("data-tip", lines.join("\n"));
}

function pushAlertsToHistory(alerts) {
  const items = (alerts || []).filter((item) => !String(item || "").toLowerCase().includes("normal"));
  if (!items.length) {
    updateAlertHistoryTooltip();
    return;
  }
  items.forEach((message) => {
    alertHistoryQueue.push({ time: getCurrentTimestamp(), message });
    if (alertHistoryQueue.length > ALERT_HISTORY_LIMIT) {
      alertHistoryQueue.shift();
    }
  });
  updateAlertHistoryTooltip();
}

function updateAlerts(alerts) {
  const box = byId("alertList");
  if (!box) return;
  box.innerHTML = "";
  (alerts || ["System status normal."]).forEach((item) => {
    const div = document.createElement("div");
    div.className = item.toLowerCase().includes("normal") ? "alert-item neutral" : "alert-item warning";
    div.textContent = item;
    box.appendChild(div);
  });
  pushAlertsToHistory(alerts);
}

function updateAdvisor(tips) {
  const box = byId("advisorList");
  if (!box) return;
  box.innerHTML = "";
  (tips || ["Collecting smart suggestions..."]).forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    box.appendChild(li);
  });
}

function updatePowerModal(data) {
  const p = data.power_breakdown || {};
  setText("pbBase", `${p.base_system ?? 0} W`);
  setText("pbDisplay", `${p.display ?? 0} W`);
  setText("pbInput", `${p.keyboard_touchpad ?? 0} W`);
  setText("pbBoard", `${p.motherboard_fans ?? 0} W`);
  setText("pbCPU", `${p.cpu ?? 0} W`);
  setText("pbGPU", `${p.gpu ?? 0} W`);
  setText("pbRAM", `${p.ram ?? 0} W`);
  setText("pbDisk", `${p.disk ?? 0} W`);
  setText("pbNetwork", `${p.network ?? 0} W`);
  setText("pbCharging", `${p.charging_overhead ?? 0} W`);
  setText("pbTotal", `${p.total ?? 0} W`);
}

function getSelectedDonutValue(data) {
  if (selectedMetric === "gpu") return data.gpu ?? 0;
  if (selectedMetric === "memory_disk") return Math.max(Number(data.ram ?? 0), Number(data.disk ?? 0));
  if (selectedMetric === "power") return data.power ?? 0;
  if (selectedMetric === "energy_total") return data.energy_total ?? 0;
  if (selectedMetric === "carbon_rate") return data.carbon_rate ?? 0;
  return data.cpu ?? 0;
}

async function fetchLiveData() {
  if (fetchInProgress) return;
  fetchInProgress = true;
  try {
    const res = await fetch(`/data?window=${currentWindow}&metric=${selectedMetric}&t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error("Failed to fetch live data");
    const data = await res.json();
    latestData = data;
    updateStaticInfo(data);
    updateCards(data);
    updateAlerts(data.alerts);
    updateAdvisor(data.advisor);
    updateMainChart(data);
    updateDonut(getSelectedDonutValue(data));
  } catch (err) {
    console.error(err);
  } finally {
    fetchInProgress = false;
  }
}

function startPolling() {
  if (pollHandle) clearInterval(pollHandle);
  fetchLiveData();
  pollHandle = setInterval(fetchLiveData, 1000);
}

function setMetric(metric, sourceEl = null) {
  selectedMetric = metric;
  document.querySelectorAll(".metric-card").forEach((card) => card.classList.remove("active-card"));
  if (sourceEl) sourceEl.classList.add("active-card");
  setText("toggleSelectedBtn", metricLabel(metric));
  fetchLiveData();
}

function setTimeRange(seconds, btn) {
  currentWindow = seconds;
  document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active-range"));
  if (btn) btn.classList.add("active-range");
  fetchLiveData();
}

function toggleSeries(seriesName) {
  if (seriesName !== "power") return;
  showPowerSeries = !showPowerSeries;
  const btn = byId("togglePowerBtn");
  if (btn) btn.classList.toggle("active-toggle", showPowerSeries);
  fetchLiveData();
}

function exportData() {
  window.location.href = "/export";
}

async function openExportedDataFolder() {
  try {
    const res = await fetch("/open_export_folder", { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.message || "Unable to open exported data folder.");
      return;
    }
    byId("themeSubmenu")?.classList.remove("show");
    byId("themeMenu")?.classList.remove("show");
  } catch (err) {
    console.error(err);
    alert("Unable to open exported data folder.");
  }
}

function openPowerModal(event) {
  if (event) event.stopPropagation();
  byId("powerModal")?.classList.add("show");
}

function closePowerModal() {
  byId("powerModal")?.classList.remove("show");
}

function hideInfoPopup() {
  const popup = byId("hoverTooltip");
  if (!popup) return;
  popup.classList.remove("show");
  popup.removeAttribute("data-owner");
}

function placeInfoPopup(dot, popup) {
  const rect = dot.getBoundingClientRect();
  const popupWidth = Math.min(300, window.innerWidth - 24);
  popup.style.maxWidth = `${popupWidth}px`;
  popup.style.left = `${window.scrollX + rect.left}px`;
  popup.style.top = `${window.scrollY + rect.bottom + 8}px`;

  requestAnimationFrame(() => {
    const popupRect = popup.getBoundingClientRect();
    let left = window.scrollX + rect.left;
    let top = window.scrollY + rect.bottom + 8;

    if (left + popupRect.width > window.scrollX + window.innerWidth - 12) {
      left = Math.max(window.scrollX + 12, window.scrollX + rect.right - popupRect.width);
    }
    if (top + popupRect.height > window.scrollY + window.innerHeight - 12) {
      top = Math.max(window.scrollY + 12, window.scrollY + rect.top - popupRect.height - 8);
    }

    popup.style.left = `${left}px`;
    popup.style.top = `${top}px`;
  });
}

function initInfoTooltips() {
  const popup = byId("hoverTooltip");
  document.querySelectorAll(".info-dot").forEach((dot) => {
    dot.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!popup) return;
      const ownerId = dot.id || "info-dot";
      const isSameOpen = popup.classList.contains("show") && popup.getAttribute("data-owner") === ownerId;
      if (isSameOpen) {
        hideInfoPopup();
        return;
      }
      popup.textContent = dot.getAttribute("data-tip") || "No details available.";
      popup.setAttribute("data-owner", ownerId);
      popup.classList.add("show");
      placeInfoPopup(dot, popup);
    });
  });
}

function applyTheme(mode) {
  const root = document.body;
  let actual = mode;
  if (mode === "system") {
    actual = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  root.setAttribute("data-theme", actual);
  localStorage.setItem("gem_theme_pref", mode);
  updateChartTheme();
}

function toggleThemeMenu(event) {
  event?.stopPropagation();
  const menu = byId("themeMenu");
  const submenu = byId("themeSubmenu");
  submenu?.classList.remove("show");
  menu?.classList.toggle("show");
}

function toggleThemeSubmenu(event) {
  event?.stopPropagation();
  byId("themeSubmenu")?.classList.toggle("show");
}

function chooseTheme(mode) {
  applyTheme(mode);
  byId("themeSubmenu")?.classList.remove("show");
  byId("themeMenu")?.classList.remove("show");
}

function initTheme() {
  const saved = localStorage.getItem("gem_theme_pref") || "light";
  applyTheme(saved);
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener?.("change", () => {
    const pref = localStorage.getItem("gem_theme_pref") || "light";
    if (pref === "system") applyTheme("system");
  });
}

window.addEventListener("click", (e) => {
  const menu = byId("themeMenu");
  const btn = byId("themeBtn");
  if (!menu || !btn) return;
  if (!menu.contains(e.target) && !btn.contains(e.target)) {
    menu.classList.remove("show");
    byId("themeSubmenu")?.classList.remove("show");
  }
});

window.addEventListener("resize", () => {
  if (mainChart) mainChart.resize();
  if (donutChart) donutChart.resize();
  hideInfoPopup();
});

window.addEventListener("scroll", hideInfoPopup, true);

document.addEventListener("click", (e) => {
  if (!e.target.closest(".info-dot") && !e.target.closest("#hoverTooltip")) {
    hideInfoPopup();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initInfoTooltips();
  initCharts();
  startPolling();
});