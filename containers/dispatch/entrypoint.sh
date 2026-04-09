#!/bin/bash
export PYTHONUNBUFFERED=1
exec python -c "
import threading, os, sys
sys.stdout.reconfigure(line_buffering=True)

# Start health check HTTP server in background thread
from http.server import HTTPServer, BaseHTTPRequestHandler
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{\"status\":\"ok\"}')
    def log_message(self, *args): pass

server = HTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8080))), Health)
threading.Thread(target=server.serve_forever, daemon=True).start()
print('Health server started on port ' + os.environ.get('PORT', '8080'), flush=True)

# Run Slack bot as main process
from henchmen.dispatch.slack_bot import main
main()
"
