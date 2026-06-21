"""Dashboard command — local web server for the trading UI."""

import threading
from pathlib import Path

import typer
from rich.console import Console

console = Console()


def dashboard(
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve on (default 8765)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open browser automatically"),
):
    """Launch the TradingAgents web dashboard in your browser."""
    import json as _json
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from cli.commands.common import _trades_path
    trades_path = _trades_path()
    html_path = Path(__file__).parent.parent / "static" / "dashboard.html"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/trades":
                data = []
                if trades_path.exists():
                    try:
                        data = _json.loads(trades_path.read_text(encoding="utf-8"))
                    except Exception:
                        data = []
                body = _json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/", "/index.html"):
                try:
                    body = html_path.read_bytes()
                except FileNotFoundError:
                    body = b"<h1>dashboard.html not found</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    url = f"http://127.0.0.1:{port}"
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        console.print(f"[red]Port {port} is already in use. Try --port <other_port>.[/red]")
        return

    console.print(f"[green]Dashboard running at {url}[/green]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    if not no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped.[/yellow]")
