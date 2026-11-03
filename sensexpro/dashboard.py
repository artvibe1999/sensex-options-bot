"""Read-only status dashboard (brief section 11): mode, feed, positions, orders, P&L, signals, exit reasons, risk
limits, kill switch. Served inside the container; docker-compose publishes it on 127.0.0.1 only (reach it with an SSH
tunnel: gcloud compute ssh sxbot -- -L 8090:127.0.0.1:8090). No controls, no secrets.
"""
from __future__ import annotations

import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)


def render(st: dict) -> str:
    def rows(items, cols):
        head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>"
                       for r in items)
        return f"<table><tr>{head}</tr>{body}</table>"

    idx = [{"index": k, **v} for k, v in (st.get("indices") or {}).items()]
    ks = st.get("kill_switch") or {}
    pos = st.get("position")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=10><title>SENSEX PRO status</title>
<style>body{{font:14px system-ui;margin:16px;background:#fff;color:#111}}table{{border-collapse:collapse;margin:8px 0;
width:100%}}td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;font-size:13px}}.bad{{color:#b00}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}td,th{{border-color:#444}}}}</style>
<h2>SENSEX PRO — {html.escape(str(st.get('mode')))}</h2>
<p>Kill switch: <b class="{'bad' if ks.get('latched') else ''}">{'LATCHED: ' + html.escape(str(ks.get('reason'))) if ks.get('latched') else 'off'}</b>
 | Day block: {html.escape(st.get('day_block') or 'none')} | Reconciled: {st.get('reconciled')}</p>
<h3>Risk</h3><pre>{html.escape(json.dumps({'book': st.get('book'), 'limits': st.get('limits')}, indent=1))}</pre>
<h3>Position</h3><pre>{html.escape(json.dumps(pos, indent=1)) if pos else 'none'}</pre>
<h3>Indices</h3>{rows(idx, ['index', 'regime', 'data_ok', 'spot', 'future_volume', 'last', 'why'])}
<h3>Pending signals</h3>{rows(st.get('pending_signals') or [], ['index', 'strategy', 'direction', 'score', 'expires'])}
<h3>Recent signals</h3>{rows(st.get('recent_signals') or [], ['ts', 'idx', 'strategy', 'direction', 'score', 'decision', 'regime'])}
<h3>Recent orders</h3>{rows(st.get('recent_orders') or [], ['created', 'client_id', 'idx', 'symbol', 'side', 'requested_qty', 'filled_qty', 'avg_price', 'state', 'reason'])}
<h3>Recent exit assessments</h3>{rows(st.get('recent_exits') or [], ['ts', 'total', 'action', 'reasons'])}
"""


class Dashboard:
    def __init__(self, host: str, port: int, status_fn: Callable[[], dict]) -> None:
        self.host, self.port, self.status_fn = host, port, status_fn
        self._srv = None

    def start(self) -> None:
        status_fn = self.status_fn

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                try:
                    st = status_fn()
                    if self.path.startswith("/status.json"):
                        body, ctype = json.dumps(st, default=str).encode(), "application/json"
                    elif self.path.startswith("/health"):
                        body, ctype = b"ok", "text/plain"
                    else:
                        body, ctype = render(st).encode(), "text/html; charset=utf-8"
                    self.send_response(200)
                except Exception:  # noqa: BLE001
                    log.exception("Dashboard render failed")
                    body, ctype = b"error", "text/plain"
                    self.send_response(500)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # quiet
                pass

        try:
            self._srv = ThreadingHTTPServer((self.host, self.port), H)
        except OSError:
            log.exception("Dashboard could not bind %s:%s (disabled)", self.host, self.port)
            return
        threading.Thread(target=self._srv.serve_forever, daemon=True, name="dashboard").start()
        log.info("Dashboard on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
