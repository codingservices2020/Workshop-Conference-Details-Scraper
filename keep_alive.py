from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.route('/')
def index():
    return "Alive"

def run():
    port = int(os.environ.get("PORT", 10000))
    try:
        # Disable the default reloader and debug logs to keep stdout clean
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        print(f"⚠️ [WARNING] Keep-alive Flask server failed to bind to port {port}: {e}")
        print("💡 The bot polling will continue running normally.")


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()