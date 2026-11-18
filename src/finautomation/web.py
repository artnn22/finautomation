"""Loopback web interface for the same calculation pipeline used by the CLI."""

import csv
import json
import re
import shutil
import tempfile
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from .main import execute_run

ASSETS = Path(__file__).with_name("web_static")
RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
ARTIFACTS = {
    "report.html", "metrics.json", "issuer_metrics.csv", "screener.csv", "formula_trace.csv",
    "scenario.csv", "alerts.csv", "forecast_next_period.csv", "parse_log.csv",
}
PREVIEW_COLUMNS = ("ticker", "source", "currency", "period_type", "period_end", "revenue", "ebitda_m", "pe_fy", "pe_ttm")
TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}
MAX_BODY = 12 * 1024 * 1024


def parse_form(data):
    if not isinstance(data, dict) or data.get("mode") not in ("demo", "csv", "online"):
        raise ValueError("Выберите режим расчёта")
    text_fields = ("tickers", "financials", "quotes", "period_dates")
    inputs = {name: data.get(name, "") for name in text_fields}
    if any(not isinstance(value, str) for value in inputs.values()):
        raise ValueError("Входные CSV должны быть текстовыми файлами UTF-8")
    mode = data["mode"]
    if mode != "demo" and not inputs["tickers"].strip():
        raise ValueError("Добавьте список тикеров")
    if mode == "csv" and not all(inputs[name].strip() for name in ("financials", "quotes")):
        raise ValueError("Добавьте CSV отчётности и котировок")
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Передайте настройки прогона")
    return mode, inputs, settings


class WebRuns:
    def __init__(self, output, examples):
        self.output = output
        self.examples = examples
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.active = None
        self.errors = {}

    def submit(self, form):
        mode, inputs, settings = parse_form(form)
        with self.lock:
            if self.active:
                raise RuntimeError("Дождитесь завершения текущего расчёта")
            run_id = uuid4().hex
            self.active = run_id
            self.pool.submit(self.calculate, run_id, mode, inputs, settings)
        return run_id

    def calculate(self, run_id, mode, inputs, settings):
        try:
            with tempfile.TemporaryDirectory(prefix="opex-web-") as temporary:
                folder = Path(temporary)
                config_file = folder / "config.yaml"
                config_file.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
                files = {}
                for name, content in inputs.items():
                    path = folder / f"{name}.csv"
                    if mode == "demo" and name != "period_dates":
                        shutil.copyfile(self.examples / path.name, path)
                    elif content.strip() and mode != "demo":
                        path.write_text(content, encoding="utf-8")
                    if path.is_file():
                        files[name] = path
                args = Namespace(
                    tickers=files["tickers"], financials=[files["financials"]] if "financials" in files else [],
                    quotes=files.get("quotes") if mode != "online" else None,
                    period_dates=files.get("period_dates") if mode == "online" else None, config=config_file,
                    out=self.output, online=mode == "online", online_financials=mode == "online",
                    synthetic_demo=mode == "demo",
                )
                execute_run(args, run_id=run_id)
        except Exception as error:
            with self.lock:
                self.errors[run_id] = str(error)
        finally:
            with self.lock:
                self.active = None

    def stats(self, run_id):
        return json.loads((self.output / run_id / "metrics.json").read_text(encoding="utf-8"))

    def history(self):
        folders = sorted(
            (folder for folder in self.output.iterdir() if RUN_ID.fullmatch(folder.name) and folder.is_dir()),
            key=lambda folder: folder.stat().st_mtime, reverse=True,
        )
        return [{"id": folder.name, "metrics": self.stats(folder.name)} for folder in folders[:20]]

    def state(self, run_id):
        with self.lock:
            if run_id in self.errors:
                return {"status": "error", "error": self.errors[run_id]}
            if self.active == run_id:
                return {"status": "running"}
        folder = self.output / run_id
        if not (folder / "metrics.json").is_file():
            return None
        with (folder / "screener.csv").open(encoding="utf-8", newline="") as source:
            preview = [{field: row[field] for field in PREVIEW_COLUMNS}
                       for row in islice(csv.DictReader(source, delimiter=";"), 8)]
        return {"status": "complete", "id": run_id, "metrics": self.stats(run_id), "screener": preview}


class WebServer(ThreadingHTTPServer):
    def __init__(self, port, runs):
        self.runs = runs
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    def same_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def headers_for(self, content_type, length, *, status=200, attachment=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.headers_for(TYPES[".json"], len(payload), status=status)
        self.wfile.write(payload)

    def file(self, path, *, download=False):
        if not path.is_file():
            self.send_json({"error": "Файл отсутствует"}, 404)
            return
        with path.open("rb") as stream:
            self.headers_for(TYPES[path.suffix], path.stat().st_size, attachment=path.name if download else None)
            shutil.copyfileobj(stream, self.wfile)

    def do_GET(self):
        if not self.same_host():
            self.send_json({"error": "Доступ только с локального адреса"}, 403)
            return
        route = urlsplit(self.path).path
        if route in {"/", "/app.css", "/app.js"}:
            self.file(ASSETS / ("index.html" if route == "/" else route[1:]))
            return
        if route == "/api/runs":
            self.send_json({"runs": self.server.runs.history()})
            return
        parts = route.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "runs"] and RUN_ID.fullmatch(parts[2]):
            state = self.server.runs.state(parts[2])
            self.send_json(state or {"error": "Запуск не найден"}, 200 if state else 404)
            return
        if len(parts) == 3 and parts[0] == "runs" and RUN_ID.fullmatch(parts[1]) and parts[2] in ARTIFACTS:
            self.file(self.server.runs.output.joinpath(*parts[1:]), download=parts[2] != "report.html")
            return
        self.send_json({"error": "Страница не найдена"}, 404)

    def do_POST(self):
        if not self.same_host() or self.headers.get("Origin") != f'http://{self.headers.get("Host")}':
            self.send_json({"error": "Запрос отклонён"}, 403)
            return
        if self.path != "/api/runs":
            self.send_json({"error": "Страница не найдена"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.send_json({"error": "Суммарный размер входных файлов больше допустимого (12 МБ)"}, 413)
                return
            if self.headers.get_content_type() != "application/json":
                self.send_json({"error": "Ожидается JSON"}, 415)
                return
            payload = json.loads(self.rfile.read(length))
            run_id = self.server.runs.submit(payload)
        except (ValueError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)
            return
        except RuntimeError as error:
            self.send_json({"error": str(error)}, 409)
            return
        self.send_json({"id": run_id, "status": "running"}, 202)


def serve(args):
    if not 1 <= args.port <= 65535:
        print("Укажите порт от 1 до 65535")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    runs = WebRuns(args.out, args.examples)
    try:
        with WebServer(args.port, runs) as server:
            print(f"Опекс доступен на http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        print(f"Ошибка интерфейса: {error}")
        return 2
    finally:
        runs.pool.shutdown()
