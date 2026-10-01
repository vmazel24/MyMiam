"""Run on the host PC; secrets never appear in terminal output or web storage."""
import argparse
import html
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mymiam.openai_plan import ChatGPTPlan, PlanError, protected_write


def run(directory, port):
    plan = ChatGPTPlan(directory)
    done = False
    attempted = False
    attempt, authorization_url = plan.authorization(f"http://127.0.0.1:{port}/auth/callback")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # OAuth callback contains an authorization code; never log it.

        def do_GET(self):
            nonlocal done, attempted
            if self.headers.get("Host") != f"127.0.0.1:{port}":
                self.send_error(400)
                return
            path = urlsplit(self.path)
            if path.path == "/":
                body = '<h1>Connecter MyMiam</h1><p>Autorise MyMiam à utiliser ton forfait.</p><a href="' + html.escape(authorization_url, quote=True) + '">Continue with ChatGPT</a>'
            elif path.path == "/auth/callback" and not attempted:
                query = {k: v[0] for k, v in parse_qs(path.query).items()}
                if not __import__('secrets').compare_digest(query.get("state", ""), attempt["state"]):
                    self.send_error(400)
                    return
                attempted = True
                try:
                    plan.exchange(attempt, query)
                    try:
                        plan.models()
                        message = "Connexion réussie. Luna est disponible. Tu peux revenir dans MyMiam."
                    except PlanError as error:
                        message = "Compte connecté. " + str(error)
                    body = "<h1>MyMiam</h1><p>" + html.escape(message) + "</p>"
                    print(message, flush=True)
                except Exception:
                    body = "<h1>Connexion non terminée</h1><p>OpenAI n'a pas confirmé cette connexion. Relance-la depuis MyMiam.</p>"
                    print("Connexion OpenAI non confirmée ; aucun secret affiché.", flush=True)
                done = True
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(("<!doctype html><html lang=fr><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>MyMiam · ChatGPT</title><body style='font:20px system-ui;max-width:650px;margin:80px auto;padding:20px'>" + body + "</body></html>").encode())

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    print(f"Connexion prête sur http://127.0.0.1:{port} (ouvrir sur ce PC).", flush=True)
    webbrowser.open(f"http://127.0.0.1:{port}")
    deadline = time.time() + 600
    while not done and time.time() < deadline:
        server.handle_request()
    server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default="instance")
    parser.add_argument("--port", type=int, default=9455)
    args = parser.parse_args()
    run(args.instance, args.port)
