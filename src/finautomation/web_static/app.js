"use strict";

const get = id => document.getElementById(id);
const form = get("run-form");
const tabs = [...document.querySelectorAll(".tab")];
const formatNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const formatPercent = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 2 });
const percentFields = new Set(["ebitda_m", "effective_tax_rate"]);
const methodLabels = {
  linear_trend: "Линейный тренд",
  recent_trend: "Тренд по недавней истории",
  seasonal_trend: "Тренд с квартальной сезонностью",
};
const files = {
  "issuer_metrics.csv": "Показатели эмитентов",
  "screener.csv": "Скринер",
  "forecast_next_period.csv": "Прогнозы по периодам",
  "formula_trace.csv": "Следы расчёта",
  "scenario.csv": "Сценарий",
  "alerts.csv": "Сигналы P/E",
  "parse_log.csv": "Журнал обработки",
  "metrics.json": "Сводка и метрики",
};
const sources = { demo: "Учебный набор", csv: "Локальные CSV", online: "Онлайн-источники" };
let current = null;
let activeTab = "screen";

const number = value => value == null || value === "" ? "—" : formatNumber.format(Number(value));
const percent = value => value == null || value === "" ? "—" : formatPercent.format(Number(value));
const date = value => new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
const output = filename => "/runs/" + current.id + "/" + filename;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function setTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  get("theme").setAttribute("aria-pressed", String(dark));
  get("theme-label").textContent = dark ? "Светлая тема" : "Тёмная тема";
  const iframe = get("report-frame").firstElementChild;
  if (iframe) iframe.style.colorScheme = dark ? "dark" : "light";
  localStorage.setItem("opex-theme", theme);
}

function addFilter() {
  const row = get("filter-template").content.firstElementChild.cloneNode(true);
  row.querySelector(".remove-filter").addEventListener("click", () => row.remove());
  get("filters").append(row);
}

function changeMode() {
  const mode = new FormData(form).get("mode");
  get("custom-inputs").hidden = mode === "demo";
  get("quotes-input").hidden = mode !== "csv";
  get("dates-input").hidden = mode !== "online";
  get("advanced").hidden = mode !== "online";
  get("financial-label").textContent = mode === "online" ? "Отчётность CSV (для MOEX или замены Yahoo)" : "Отчётность CSV";
  get("mode-note").textContent = {
    demo: "8 учебных эмитентов. Расчёт работает без сети.",
    csv: "Приложите CSV со своими данными.",
    online: "Котировки MOEX ISS и Yahoo Finance. Отчётность Yahoo загружается автоматически.",
  }[mode];
}

async function fileText(id) {
  const file = get(id).files[0];
  if (!file) return "";
  if (file.size > 12 * 1024 * 1024) throw new Error("Размер входного файла превышает 12 МБ.");
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  } catch (error) {
    throw new Error("Сохраните входной CSV в кодировке UTF-8.");
  }
}

function boards() {
  const entries = [];
  for (const line of get("boards").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
    const parts = line.split("=").map(value => value.trim());
    if (parts.length !== 2 || !parts[0] || !/^[A-Z0-9]+$/.test(parts[1])) {
      throw new Error("Укажите режимы MOEX в формате SBER=TQBR.");
    }
    entries.push(parts);
  }
  return Object.fromEntries(entries);
}

async function collectForm() {
  const mode = new FormData(form).get("mode");
  const settings = {
    filters: [...get("filters").children].map(row => {
      const field = row.querySelector(".field").value;
      const value = row.querySelector(".limit").valueAsNumber;
      return { field, op: row.querySelector(".operator").value, value: percentFields.has(field) ? value / 100 : value };
    }),
    scenario: {
      r: get("revenue-change").valueAsNumber / 100,
      m: get("margin-change").valueAsNumber / 100,
      delta_d: get("debt-change").valueAsNumber,
    },
    forecast: { min_train_periods: get("min-periods").valueAsNumber },
    quotes: { timeout_seconds: get("timeout").valueAsNumber, moex_boards: mode === "online" ? boards() : {} },
  };
  if (mode === "demo") return { mode, settings };
  let tickers = get("tickers").value.trim();
  if (tickers && get("tickers-file").files.length) throw new Error("Выберите один способ ввода тикеров.");
  if (tickers && !/^ticker\s*[,;]\s*market\b/i.test(tickers)) {
    const delimiter = tickers.split("\n", 1)[0].includes(";") ? ";" : ",";
    tickers = "ticker" + delimiter + "market\n" + tickers;
  }
  return {
    mode, settings, tickers: tickers || await fileText("tickers-file"),
    financials: await fileText("financials"),
    quotes: mode === "csv" ? await fileText("quotes") : "",
    period_dates: mode === "online" ? await fileText("period-dates") : "",
  };
}

async function request(url, init) {
  const response = await fetch(url, init);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error ?? "Не удалось выполнить запрос.");
  return body;
}

function changeState(status, caption) {
  get("run-state").className = "run-state " + status;
  get("run-state").lastElementChild.textContent = {
    idle: "Ожидает запуска", running: "Выполняется", complete: "Готово", error: "Ошибка",
  }[status];
  get("run-caption").textContent = caption;
}

function renderPreview(rows) {
  const body = get("screen-body");
  body.replaceChildren();
  for (const row of rows) {
    const tr = element("tr");
    const instrument = element("td", row.ticker);
    instrument.append(element("small", row.source.toUpperCase() + " · " + row.currency));
    tr.append(instrument, element("td", row.period_type + " · " + row.period_end),
      element("td", number(row.revenue)), element("td", percent(row.ebitda_m)),
      element("td", number(row.period_type === "FY" ? row.pe_fy : row.pe_ttm)));
    body.append(tr);
  }
  if (!rows.length) {
    const tr = element("tr");
    const cell = element("td", "По заданным условиям совпадений нет.");
    cell.colSpan = 5;
    tr.append(cell);
    body.append(tr);
  }
}

function renderForecast() {
  const frequency = document.querySelector('input[name="frequency"]:checked').value;
  const details = current.metrics.forecast.by_period_type[frequency];
  const measures = [
    ["Выбранная модель", details.mape_model],
    ["Базовый линейный тренд", details.mape_linear_reference],
    ["Naive · последнее значение", details.mape_naive],
  ];
  const ceiling = Math.max(...measures.map(([, value]) => value || 0), .01);
  const container = get("forecast-values");
  container.replaceChildren();
  for (const [label, value] of measures) {
    const measure = element("div", undefined, "measure");
    const title = element("div");
    title.append(element("span", label), element("strong", percent(value)));
    const track = element("div", undefined, "track");
    const bar = element("i");
    bar.style.width = (value ? 100 * value / ceiling : 0) + "%";
    track.append(bar);
    measure.append(title, track);
    container.append(measure);
  }
  const labels = Object.keys(current.metrics.forecast.selected_models ?? {}).map(name => methodLabels[name] ?? name);
  const note = details.eligible_points ? "Допустимых точек: " + details.eligible_points + ". Чем ниже MAPE, тем меньше ошибка."
    : "Для этой частоты недостаточно точек для оценки.";
  get("forecast-note").textContent = note + (labels.length ? " Модели в запуске: " + labels.join(", ") + "." : "");
}

function showTab(name) {
  activeTab = name;
  for (const tab of tabs) {
    const active = name === tab.dataset.tab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-pressed", String(active));
    get(tab.dataset.tab + "-pane").hidden = !current || !active;
  }
  get("empty").hidden = Boolean(current);
  if (!current) return;
  if (name === "forecast") renderForecast();
  if (name === "report" && !get("report-frame").firstChild) {
    const iframe = element("iframe");
    iframe.title = "Полный отчёт Опекс";
    iframe.setAttribute("sandbox", "allow-downloads");
    iframe.style.colorScheme = document.documentElement.dataset.theme;
    iframe.src = output("report.html");
    get("report-frame").append(iframe);
  }
}

function showRun(result) {
  current = result;
  get("report-frame").replaceChildren();
  const stats = result.metrics;
  const source = stats.synthetic_demo ? sources.demo : stats.quote_mode === "online" ? sources.online : sources.csv;
  changeState("complete", source + " · " + date(stats.started_at));
  get("matched").textContent = stats.ingestion.with_quotes + " / " + stats.ingestion.requested;
  get("coverage").textContent = stats.ingestion.coverage == null ? "Доля обработанного списка" : percent(stats.ingestion.coverage) + " охвата";
  get("passed").textContent = stats.screener.passed_instruments;
  get("mape").textContent = percent(stats.forecast.mape_model);
  get("points").textContent = "Контрольных точек: " + stats.forecast.eligible_points;
  get("open-window").href = output("report.html");
  get("open-window").hidden = false;
  renderPreview(result.screener);
  get("files").replaceChildren(...Object.entries(files).map(([filename, label]) => {
    const item = element("li");
    const link = element("a", label + " ↓", "file-link");
    link.href = output(filename);
    link.download = filename;
    item.append(link);
    return item;
  }));
  showTab(activeTab);
}

function setFormStatus(message = "", error = false) {
  get("form-status").textContent = message;
  get("form-status").classList.toggle("error", error);
}

async function submit(event) {
  event.preventDefault();
  get("calculate").disabled = true;
  setFormStatus("Подготовка входных данных…");
  try {
    const payload = await collectForm();
    const started = await request("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    current = null;
    get("report-frame").replaceChildren();
    get("open-window").hidden = true;
    for (const id of ["matched", "passed", "mape"]) get(id).textContent = "—";
    get("coverage").textContent = "Доля обработанного списка";
    get("points").textContent = "По контрольным периодам";
    changeState("running", "Идёт обработка данных");
    get("empty-title").textContent = "Расчёт выполняется";
    get("empty-text").textContent = "Сводка появится здесь, когда будут готовы результаты.";
    showTab("screen");
    setFormStatus("Расчёт выполняется…");
    let result;
    do {
      await new Promise(resolve => setTimeout(resolve, 650));
      result = await request("/api/runs/" + started.id);
    } while (result.status === "running");
    if (result.status === "error") throw new Error(result.error);
    showRun(result);
    setFormStatus(result.metrics.ingestion.processed ? "Расчёт завершён." : "Пригодных строк нет. Проверьте журнал.");
  } catch (error) {
    setFormStatus(error.message, true);
    if (!current) {
      changeState("error", "Проверьте входные данные и параметры");
      get("empty-title").textContent = "Расчёт не завершён";
      get("empty-text").textContent = error.message;
    }
  } finally {
    get("calculate").disabled = false;
  }
}

function navigate(view) {
  get("workspace").hidden = view === "history";
  get("history-view").hidden = view !== "history";
  for (const name of ["work", "history"]) {
    get("nav-" + name).classList.toggle("active", name === view);
    if (name === view) get("nav-" + name).setAttribute("aria-current", "page");
    else get("nav-" + name).removeAttribute("aria-current");
  }
}

async function showHistory() {
  navigate("history");
  const container = get("history-list");
  container.textContent = "Загрузка истории…";
  try {
    const { runs } = await request("/api/runs");
    if (!runs.length) {
      container.textContent = "Завершённых расчётов пока нет.";
      return;
    }
    const list = element("ul");
    for (const run of runs) {
      const item = element("li", undefined, "history-entry");
      const summary = element("div");
      summary.append(element("strong", date(run.metrics.started_at)), element("br"),
        element("small", "С котировками: " + run.metrics.ingestion.with_quotes + " · Отобрано: " + run.metrics.screener.passed_instruments));
      const open = element("button", "Открыть", "button outline");
      open.type = "button";
      open.addEventListener("click", async () => {
        try {
          const result = await request("/api/runs/" + run.id);
          activeTab = "screen";
          showRun(result);
          navigate("work");
        } catch (error) {
          container.prepend(element("p", error.message, "hint"));
        }
      });
      item.append(summary, open);
      list.append(item);
    }
    container.replaceChildren(list);
  } catch (error) {
    container.textContent = error.message;
  }
}

setTheme(localStorage.getItem("opex-theme") ?? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
get("theme").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
get("add-filter").addEventListener("click", addFilter);
for (const mode of form.querySelectorAll('input[name="mode"]')) mode.addEventListener("change", changeMode);
for (const tab of tabs) tab.addEventListener("click", () => showTab(tab.dataset.tab));
for (const frequency of document.querySelectorAll('input[name="frequency"]')) frequency.addEventListener("change", renderForecast);
get("nav-work").addEventListener("click", () => navigate("work"));
get("nav-history").addEventListener("click", showHistory);
get("new-run").addEventListener("click", () => navigate("work"));
form.addEventListener("submit", submit);
addFilter();
