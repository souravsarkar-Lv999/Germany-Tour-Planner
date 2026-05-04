from __future__ import annotations

import functools
import http.server
import json
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import qrcode
import qrcode.image.svg


ROOT = Path(__file__).resolve().parent
START_PORT = 8507
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_FILE = ROOT / "secrets" / "openrouter_api_key.txt"
PHONE_URL = ""
LOCAL_URL = ""


SYSTEM_PROMPT = """You are a friendly AI travel assistant inside a local Germany tour planner app.
Help the user plan a beautiful English-language trip across Berlin, Hamburg, Bremen, Frankfurt,
Cologne, Munich, and Dresden. Keep answers practical, local, concise, and easy to follow.
Suggest routes, food, neighborhoods, photo spots, train rhythm, and day plans. If asked for
bookings, prices, closures, or live schedules, remind the user to verify current details."""


def read_api_key() -> str:
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def json_response(handler: http.server.BaseHTTPRequestHandler, status: int, data: dict) -> None:
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def svg_response(handler: http.server.BaseHTTPRequestHandler, svg: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "image/svg+xml; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(svg)))
    handler.end_headers()
    handler.wfile.write(svg)


def get_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())


class TravelPlannerHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/api/info":
            json_response(self, 200, {"localUrl": LOCAL_URL, "phoneUrl": PHONE_URL})
            return

        if self.path == "/qr.svg":
            qr = qrcode.QRCode(border=2, box_size=8)
            qr.add_data(PHONE_URL or LOCAL_URL)
            qr.make(fit=True)
            image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
            svg_response(self, image.to_string())
            return

        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            json_response(self, 404, {"error": "Unknown API route."})
            return

        api_key = read_api_key()
        if not api_key or "PASTE" in api_key.upper():
            json_response(
                self,
                400,
                {
                    "error": (
                        "Missing API key. Paste your OpenRouter API key into "
                        "secrets/openrouter_api_key.txt, save it, then run app.py again."
                    )
                },
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(min(content_length, 12000))
            payload = json.loads(raw_body.decode("utf-8"))
            user_message = str(payload.get("message", "")).strip()
            history = payload.get("history", [])
        except (ValueError, json.JSONDecodeError):
            json_response(self, 400, {"error": "Invalid chat request."})
            return

        if not user_message:
            json_response(self, 400, {"error": "Type a message first."})
            return

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if isinstance(history, list):
            for item in history[-8:]:
                role = item.get("role")
                content = str(item.get("content", "")).strip()
                if role in {"user", "assistant"} and content:
                    messages.append({"role": role, "content": content[:1800]})
        messages.append({"role": "user", "content": user_message[:2500]})

        request_body = json.dumps(
            {
                "model": MODEL,
                "messages": messages,
                "temperature": 0.65,
                "max_tokens": 800,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            OPENROUTER_URL,
            data=request_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://127.0.0.1",
                "X-Title": "Germany Tour Planner",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.loads(response.read().decode("utf-8"))
            answer = data["choices"][0]["message"]["content"].strip()
            json_response(self, 200, {"reply": answer})
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            json_response(self, error.code, {"error": f"OpenRouter error: {detail}"})
        except Exception as error:
            json_response(self, 500, {"error": f"Chat failed: {error}"})


def find_free_port(start: int = START_PORT) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local port found between 8507 and 8556.")


def main() -> None:
    global LOCAL_URL, PHONE_URL

    index = ROOT / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"Missing website file: {index}")

    port = find_free_port()
    lan_ip = get_lan_ip()
    LOCAL_URL = f"http://127.0.0.1:{port}/index.html"
    PHONE_URL = f"http://{lan_ip}:{port}/index.html"
    handler = functools.partial(TravelPlannerHandler, directory=str(ROOT))

    class ReusableServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableServer(("0.0.0.0", port), handler) as server:
        print()
        print("Germany Tour Planner is running.")
        print(f"Open on this computer: {LOCAL_URL}")
        print(f"Open on your phone:    {PHONE_URL}")
        print("Your phone must be on the same Wi-Fi/network as this computer.")
        print("Press Ctrl+C in this terminal to stop the website.")
        print()

        threading.Thread(
            target=lambda: (time.sleep(0.8), webbrowser.open(LOCAL_URL)),
            daemon=True,
        ).start()

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nWebsite stopped.")


if __name__ == "__main__":
    main()
