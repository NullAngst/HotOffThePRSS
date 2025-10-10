# main_web.py
# A Discord RSS bot with a multi-page web interface for configuration.
# This version is for the web UI only. The scheduler runs as a separate process.

import os
import json
import uuid
import yaml
from datetime import datetime, timezone
from flask import Flask, render_template_string, request, redirect, url_for, flash, get_flashed_messages, send_file, session, g
from werkzeug.security import generate_password_hash, check_password_hash

# --- Set a common User-Agent for all feedparser requests ---
import feedparser
feedparser.USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/116.0"

# --- Flask Web App Setup ---
app = Flask(__name__)

# --- Configuration & State Files ---
CONFIG_FILE = "config.json"
SENT_ARTICLES_FILE = "sent_articles.yaml"
FEED_STATE_FILE = "feed_state.json"
USER_FILE = "user.json" # Stores the admin user's credentials
SECRET_KEY_FILE = "secret.key" # Stores the Flask secret key

# --- Helper function for human-readable time ---
def time_ago(dt_str):
    if not dt_str:
        return "never"
    dt = datetime.fromisoformat(dt_str)
    now = datetime.now(timezone.utc)
    diff = now - dt
    
    seconds = diff.total_seconds()
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    elif seconds < 86400:
        return f"{int(seconds / 3600)} hours ago"
    else:
        return f"{int(seconds / 86400)} days ago"

# Make the helper function available in templates
app.jinja_env.globals.update(time_ago=time_ago)

# --- HTML Templates ---

LAYOUT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Discord RSS Bot Control Panel</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📡</text></svg>">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style> body { font-family: 'Inter', sans-serif; } </style>
</head>
<body class="bg-gray-900 text-white">
    <div class="container mx-auto p-4 md:p-8 max-w-7xl">
        <div class="flex justify-between items-center mb-2">
            <h1 class="text-3xl font-bold text-center flex-grow">Discord RSS Bot Control Panel</h1>
            {% if g.user %}
                <a href="{{ url_for('logout') }}" class="text-gray-400 hover:text-white transition-colors text-sm">Logout</a>
            {% endif %}
        </div>
        
        {% if g.user %}
        <nav class="flex justify-center space-x-6 bg-gray-800 p-4 rounded-xl shadow-lg mb-6">
            <a href="{{ url_for('view_feeds') }}" class="text-gray-300 hover:text-white transition-colors">View Feeds</a>
            <a href="{{ url_for('add_feed') }}" class="text-gray-300 hover:text-white transition-colors">Add New Feed</a>
            <a href="{{ url_for('backup_restore') }}" class="text-gray-300 hover:text-white transition-colors">Backup / Restore</a>
        </nav>
        {% endif %}

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="bg-{{ 'green' if category == 'success' else 'red' }}-500/20 border border-{{ 'green' if category == 'success' else 'red' }}-500 text-{{ 'green' if category == 'success' else 'red' }}-300 px-4 py-3 rounded-lg relative mb-6" role="alert">
                    <span class="block sm:inline">{{ message }}</span>
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <main>
            {% block content %}{% endblock %}
        </main>
    </div>
</body>
</html>
"""

SETUP_TEMPLATE = """
<div class="bg-gray-800 p-6 rounded-xl shadow-lg max-w-md mx-auto">
    <h2 class="text-2xl font-semibold mb-4 text-center">Create Admin Account</h2>
    <p class="text-center text-gray-400 mb-6">Welcome! As this is the first time running the application, please create an admin account to secure the control panel.</p>
    <form method="post">
        <div class="mb-4">
            <label for="username" class="block text-gray-300 text-sm font-bold mb-2">Username</label>
            <input type="text" name="username" id="username" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline" required>
        </div>
        <div class="mb-6">
            <label for="password" class="block text-gray-300 text-sm font-bold mb-2">Password</label>
            <input type="password" name="password" id="password" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline" required>
        </div>
        <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
            Create Account
        </button>
    </form>
</div>
"""

LOGIN_TEMPLATE = """
<div class="bg-gray-800 p-6 rounded-xl shadow-lg max-w-md mx-auto">
    <h2 class="text-2xl font-semibold mb-4 text-center">Admin Login</h2>
    <form method="post">
        <div class="mb-4">
            <label for="username" class="block text-gray-300 text-sm font-bold mb-2">Username</label>
            <input type="text" name="username" id="username" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline" required>
        </div>
        <div class="mb-6">
            <label for="password" class="block text-gray-300 text-sm font-bold mb-2">Password</label>
            <input type="password" name="password" id="password" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline" required>
        </div>
        <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
            Login
        </button>
    </form>
</div>
"""

VIEW_FEEDS_TEMPLATE = """
<div class="bg-gray-800 p-6 rounded-xl shadow-lg">
    <div class="flex justify-between items-center mb-4">
        <h2 class="text-2xl font-semibold">Existing Feeds</h2>
        <a href="{{ url_for('add_feed') }}" class="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
            Add New Feed
        </a>
    </div>
    <div class="overflow-x-auto">
        <table class="min-w-full text-left text-sm font-light">
            <thead class="border-b border-gray-600 font-medium">
                <tr>
                    <th scope="col" class="px-6 py-4">Feed Status</th>
                    <th scope="col" class="px-6 py-4">Last Post Status</th>
                    <th scope="col" class="px-6 py-4">RSS Name</th>
                    <th scope="col" class="px-6 py-4">Feed URL</th>
                    <th scope="col" class="px-6 py-4">Webhook Destinations</th>
                    <th scope="col" class="px-6 py-4">Interval (s)</th>
                    <th scope="col" class="px-6 py-4">Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for feed in config.FEEDS %}
                {% set state = feed_state.get(feed.id, {}) %}
                {% set status_code = state.get('status_code') %}
                {% set last_post = state.get('last_post', {}) %}
                {% set post_status = last_post.get('status') %}
                {% set post_time = last_post.get('timestamp') %}
                <tr class="border-b border-gray-700">
                    <td class="px-6 py-4 font-bold">
                        {% if status_code %}
                            {% if 200 <= status_code < 300 %}
                                <span class="text-green-400">{{ status_code }} OK</span>
                            {% elif 300 <= status_code < 400 %}
                                <span class="text-yellow-400">{{ status_code }} Redirect</span>
                            {% else %}
                                <span class="text-red-400">{{ status_code }} Error</span>
                            {% endif %}
                        {% else %}
                            <span class="text-gray-500">Pending</span>
                        {% endif %}
                    </td>
                    <td class="px-6 py-4">
                        {% if post_status %}
                            {% if post_status == 'Success' %}
                                <span class="text-green-400 font-semibold">{{ post_status }}</span>
                            {% elif post_status == 'Rate Limited' %}
                                <span class="text-yellow-400 font-semibold">{{ post_status }}</span>
                            {% else %}
                                <span class="text-red-400 font-semibold">{{ post_status }}</span>
                            {% endif %}
                            <p class="text-gray-500 text-xs">{{ time_ago(post_time) }}</p>
                        {% else %}
                            <span class="text-gray-500">No posts yet</span>
                        {% endif %}
                    </td>
                    <td class="px-6 py-4 text-gray-300 truncate" style="max-width: 250px;">{{ feed.get('name', 'Not Set') }}</td>
                    <td class="px-6 py-4 font-mono text-xs truncate" style="max-width: 300px;">{{ feed.url }}</td>
                    <td class="px-6 py-4 text-xs text-gray-400">
                        {% set webhooks = feed.get('webhooks', []) %}
                        {% if webhooks %}
                            {{ webhooks|map(attribute='label')|join(', ') }}
                        {% else %}
                            {{ feed.get('webhook_urls', [])|length }} legacy URL(s)
                        {% endif %}
                    </td>
                    <td class="px-6 py-4">{{ feed.update_interval }}</td>
                    <td class="px-6 py-4 flex items-center space-x-4">
                        <a href="{{ url_for('edit_feed', feed_id=feed.id) }}" class="text-indigo-400 hover:text-indigo-300">Edit</a>
                        <form action="{{ url_for('delete_feed', feed_id=feed.id) }}" method="post" onsubmit="return confirm('Are you sure you want to delete this feed?');">
                            <button type="submit" class="text-red-500 hover:text-red-400">Delete</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="7" class="text-center py-8 text-gray-400">No feeds configured. <a href="{{ url_for('add_feed') }}" class="text-indigo-400 hover:underline">Add one now!</a></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
"""

FORM_TEMPLATE_SHARED_SCRIPT = """
<script>
    function addWebhookRow() {
        const container = document.getElementById('webhook-container');
        const newRow = document.createElement('div');
        newRow.className = 'flex items-center space-x-2 mb-2';
        newRow.innerHTML = `
            <input type="url" name="webhook_url" placeholder="Webhook URL" class="flex-grow shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500" required>
            <input type="text" name="webhook_label" placeholder="Label (e.g., Server - #channel)" class="w-1/3 shadow appearance-none border border-gray-700 rounded-lg py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500">
            <button type="button" onclick="this.parentElement.remove()" class="bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-2 rounded-lg" title="Remove Webhook">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-trash" viewBox="0 0 16 16">
                    <path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/>
                    <path fill-rule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/>
                </svg>
            </button>
        `;
        container.appendChild(newRow);
    }
</script>
"""

ADD_FEED_TEMPLATE = """
<div class="bg-gray-800 p-6 rounded-xl shadow-lg">
    <h2 class="text-2xl font-semibold mb-4">Add a New Feed</h2>
    <form action="{{ url_for('add_feed') }}" method="post">
        <div class="mb-4">
            <label for="name" class="block text-gray-300 text-sm font-bold mb-2">RSS Name (Optional)</label>
            <input type="text" name="name" id="name" placeholder="e.g., Tech News" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500">
        </div>
        <div class="mb-4">
            <label for="url" class="block text-gray-300 text-sm font-bold mb-2">RSS Feed URL</label>
            <input type="url" name="url" id="url" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500" required>
        </div>
        <div class="mb-4">
            <label class="block text-gray-300 text-sm font-bold mb-2">Discord Webhook Destinations</label>
            <div id="webhook-container">
                <!-- Dynamic rows will be inserted here -->
            </div>
            <button type="button" onclick="addWebhookRow()" class="mt-2 text-sm bg-gray-700 hover:bg-gray-600 text-indigo-400 font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
                + Add Webhook
            </button>
        </div>
        <div class="mb-6">
            <label for="update_interval" class="block text-gray-300 text-sm font-bold mb-2">Refresh Interval (seconds)</label>
            <input type="number" name="update_interval" id="update_interval" value="300" min="60" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500" required>
        </div>
        <button type="submit" class="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
            Add Feed
        </button>
    </form>
</div>
{{ FORM_TEMPLATE_SHARED_SCRIPT | safe }}
<script>
    // Add one row by default on page load for a new feed
    document.addEventListener('DOMContentLoaded', function() {
        addWebhookRow();
    });
</script>
"""

EDIT_FEED_TEMPLATE = """
<div class="bg-gray-800 p-6 rounded-xl shadow-lg">
    <h2 class="text-2xl font-semibold mb-4">Edit Feed</h2>
    <form action="{{ url_for('edit_feed', feed_id=feed.id) }}" method="post">
        <div class="mb-4">
            <label for="name" class="block text-gray-300 text-sm font-bold mb-2">RSS Name (Optional)</label>
            <input type="text" name="name" id="name" value="{{ feed.get('name', '') }}" placeholder="e.g., Tech News" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500">
        </div>
        <div class="mb-4">
            <label for="url" class="block text-gray-300 text-sm font-bold mb-2">RSS Feed URL</label>
            <input type="url" name="url" id="url" value="{{ feed.url }}" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500" required>
        </div>
        <div class="mb-4">
            <label class="block text-gray-300 text-sm font-bold mb-2">Discord Webhook Destinations</label>
            <div id="webhook-container">
                {% for webhook in feed.get('webhooks', []) %}
                <div class="flex items-center space-x-2 mb-2">
                    <input type="url" name="webhook_url" placeholder="Webhook URL" value="{{ webhook.url }}" class="flex-grow shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500" required>
                    <input type="text" name="webhook_label" placeholder="Label (e.g., Server - #channel)" value="{{ webhook.label }}" class="w-1/3 shadow appearance-none border border-gray-700 rounded-lg py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500">
                    <button type="button" onclick="this.parentElement.remove()" class="bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-2 rounded-lg" title="Remove Webhook">
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-trash" viewBox="0 0 16 16"><path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/><path fill-rule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/></svg>
                    </button>
                </div>
                {% endfor %}
            </div>
            <button type="button" onclick="addWebhookRow()" class="mt-2 text-sm bg-gray-700 hover:bg-gray-600 text-indigo-400 font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
                + Add Webhook
            </button>
        </div>
        <div class="mb-6">
            <label for="update_interval" class="block text-gray-300 text-sm font-bold mb-2">Refresh Interval (seconds)</label>
            <input type="number" name="update_interval" id="update_interval" value="{{ feed.update_interval }}" min="60" class="shadow appearance-none border border-gray-700 rounded-lg w-full py-2 px-3 bg-gray-700 text-gray-200 leading-tight focus:outline-none focus:shadow-outline focus:border-indigo-500" required>
        </div>
        <div class="flex items-center space-x-4">
            <button type="submit" class="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
                Save Changes
            </button>
            <a href="{{ url_for('view_feeds') }}" class="text-gray-400 hover:text-white">Cancel</a>
        </div>
    </form>
</div>
{{ FORM_TEMPLATE_SHARED_SCRIPT | safe }}
"""

BACKUP_RESTORE_TEMPLATE = """
<div class="bg-gray-800 p-6 rounded-xl shadow-lg">
    <h2 class="text-2xl font-semibold mb-4">Backup & Restore</h2>
    <p class="text-gray-400 mb-6">Download your current feed configuration or restore from a backup file.</p>
    <div class="mb-6">
        <h3 class="text-lg font-medium mb-2">Download Backup</h3>
        <p class="text-gray-400 text-sm mb-3">Saves a copy of your `config.json` file containing all your feeds.</p>
        <a href="{{ url_for('download_backup') }}" class="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200 inline-block">
            Download config.json
        </a>
    </div>
    <hr class="border-gray-700 my-6">
    <div>
        <h3 class="text-lg font-medium mb-2">Restore from Backup</h3>
        <p class="text-gray-400 text-sm mb-3">Upload a `config.json` file to restore your feeds. This will overwrite your current configuration.</p>
        <form action="{{ url_for('upload_backup') }}" method="post" enctype="multipart/form-data">
            <div class="flex items-center space-x-4">
                <input type="file" name="backup_file" accept=".json" required class="block w-full text-sm text-gray-400 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-indigo-600 file:text-white hover:file:bg-indigo-700">
                <button type="submit" class="bg-green-600 hover:bg-green-700 text-white font-bold py-2 px-4 rounded-lg focus:outline-none focus:shadow-outline transition-colors duration-200">
                    Upload & Restore
                </button>
            </div>
        </form>
    </div>
</div>
"""

# This dictionary will act as a simple template loader.
TEMPLATES = {
    "layout": LAYOUT_TEMPLATE,
    "view_feeds": VIEW_FEEDS_TEMPLATE,
    "add_feed": ADD_FEED_TEMPLATE,
    "edit_feed": EDIT_FEED_TEMPLATE,
    "backup_restore": BACKUP_RESTORE_TEMPLATE,
    "setup": SETUP_TEMPLATE,
    "login": LOGIN_TEMPLATE,
    "form_shared_script": FORM_TEMPLATE_SHARED_SCRIPT
}

# --- Configuration and State Management ---

def save_config(config_data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=4)

def initialize_files():
    """Ensure all necessary files exist before the app starts."""
    if not os.path.exists(CONFIG_FILE):
        save_config({"FEEDS": []})
    if not os.path.exists(SENT_ARTICLES_FILE):
        with open(SENT_ARTICLES_FILE, 'w') as f: yaml.dump([], f)
    if not os.path.exists(FEED_STATE_FILE):
        with open(FEED_STATE_FILE, 'w') as f: json.dump({}, f)
    # User file is checked separately by the auth logic

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def load_feed_state():
    try:
        with open(FEED_STATE_FILE, 'r') as f:
            content = f.read()
            if not content: return {}
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# --- Initialize files on application startup ---
initialize_files()

# --- Authentication Logic ---

def get_secret_key():
    """Generates a secret key and saves it, or loads the existing one."""
    if not os.path.exists(SECRET_KEY_FILE):
        print("Generating new secret key...")
        key = os.urandom(24)
        with open(SECRET_KEY_FILE, 'wb') as f:
            f.write(key)
        return key
    else:
        with open(SECRET_KEY_FILE, 'rb') as f:
            return f.read()

app.secret_key = get_secret_key()

def admin_user_exists():
    return os.path.exists(USER_FILE)

def get_admin_user():
    """Safely loads the admin user from the JSON file."""
    if not os.path.exists(USER_FILE):
        return None
    try:
        with open(USER_FILE, 'r') as f:
            content = f.read()
            if not content:
                return None
            return json.loads(content)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

@app.before_request
def load_logged_in_user():
    user_id = session.get('user_id')
    g.user = get_admin_user() if user_id else None

@app.before_request
def require_login_or_setup():
    if not admin_user_exists() and request.endpoint != 'setup':
        return redirect(url_for('setup'))
    
    if admin_user_exists() and g.user is None and request.endpoint not in ['login', 'setup']:
        return redirect(url_for('login'))

# --- Flask Routes ---

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if admin_user_exists():
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user_data = {
            "id": 1,
            "username": username,
            "password": generate_password_hash(password)
        }
        with open(USER_FILE, 'w') as f:
            json.dump(user_data, f)
        
        flash('Admin account created successfully! Please log in.', 'success')
        return redirect(url_for('login'))

    full_html = TEMPLATES["layout"].replace('{% block content %}{% endblock %}', TEMPLATES["setup"])
    return render_template_string(full_html)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('view_feeds'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = get_admin_user()
        error = "Invalid username or password."

        if user and user.get('username') == username and check_password_hash(user.get('password', ''), password):
            session.clear()
            session['user_id'] = user['id']
            return redirect(url_for('view_feeds'))
        else:
            flash(error, 'error')

    full_html = TEMPLATES["layout"].replace('{% block content %}{% endblock %}', TEMPLATES["login"])
    return render_template_string(full_html)

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/')
def view_feeds():
    config = load_config()
    feed_state = load_feed_state()
    full_html = TEMPLATES["layout"].replace('{% block content %}{% endblock %}', TEMPLATES["view_feeds"])
    return render_template_string(full_html, config=config, feed_state=feed_state)

@app.route('/add', methods=['GET', 'POST'])
def add_feed():
    if request.method == 'POST':
        config = load_config()
        webhook_urls = request.form.getlist('webhook_url')
        webhook_labels = request.form.getlist('webhook_label')
        
        webhooks_data = [
            {"url": url, "label": label}
            for url, label in zip(webhook_urls, webhook_labels) if url
        ]

        new_feed = {
            "id": str(uuid.uuid4()),
            "name": request.form['name'],
            "url": request.form['url'],
            "webhooks": webhooks_data,
            "update_interval": int(request.form['update_interval'])
        }
        config['FEEDS'].append(new_feed)
        save_config(config)
        flash(f'Feed "{new_feed["url"]}" added successfully!', 'success')
        return redirect(url_for('view_feeds'))
    
    full_html = TEMPLATES["layout"].replace('{% block content %}{% endblock %}', TEMPLATES["add_feed"])
    return render_template_string(full_html, FORM_TEMPLATE_SHARED_SCRIPT=TEMPLATES["form_shared_script"])

@app.route('/edit/<feed_id>', methods=['GET', 'POST'])
def edit_feed(feed_id):
    config = load_config()
    feed_to_edit = next((feed for feed in config['FEEDS'] if feed['id'] == feed_id), None)

    if feed_to_edit is None:
        flash('Feed not found.', 'error')
        return redirect(url_for('view_feeds'))

    # Handle legacy format for backward compatibility
    if 'webhook_urls' in feed_to_edit and 'webhooks' not in feed_to_edit:
        feed_to_edit['webhooks'] = [{"url": url, "label": ""} for url in feed_to_edit['webhook_urls']]

    if request.method == 'POST':
        webhook_urls = request.form.getlist('webhook_url')
        webhook_labels = request.form.getlist('webhook_label')
        
        webhooks_data = [
            {"url": url, "label": label}
            for url, label in zip(webhook_urls, webhook_labels) if url
        ]

        for i, feed in enumerate(config['FEEDS']):
            if feed['id'] == feed_id:
                config['FEEDS'][i]['name'] = request.form['name']
                config['FEEDS'][i]['url'] = request.form['url']
                config['FEEDS'][i]['webhooks'] = webhooks_data
                config['FEEDS'][i]['update_interval'] = int(request.form['update_interval'])
                # Clean up legacy fields if they exist
                config['FEEDS'][i].pop('webhook_url', None)
                config['FEEDS'][i].pop('webhook_urls', None)
                break
        
        save_config(config)
        flash(f'Feed updated successfully!', 'success')
        return redirect(url_for('view_feeds'))

    full_html = TEMPLATES["layout"].replace('{% block content %}{% endblock %}', TEMPLATES["edit_feed"])
    return render_template_string(full_html, feed=feed_to_edit, FORM_TEMPLATE_SHARED_SCRIPT=TEMPLATES["form_shared_script"])

@app.route('/delete/<feed_id>', methods=['POST'])
def delete_feed(feed_id):
    config = load_config()
    feed_to_delete = next((feed for feed in config['FEEDS'] if feed['id'] == feed_id), None)
    if feed_to_delete:
        config['FEEDS'] = [feed for feed in config['FEEDS'] if feed['id'] != feed_id]
        save_config(config)
        flash(f'Feed "{feed_to_delete["url"]}" deleted.', 'success')
    else:
        flash('Feed not found.', 'error')
    return redirect(url_for('view_feeds'))

@app.route('/backup-restore')
def backup_restore():
    full_html = TEMPLATES["layout"].replace('{% block content %}{% endblock %}', TEMPLATES["backup_restore"])
    return render_template_string(full_html)

@app.route('/backup/download')
def download_backup():
    return send_file(CONFIG_FILE, as_attachment=True)

@app.route('/backup/upload', methods=['POST'])
def upload_backup():
    if 'backup_file' not in request.files:
        flash('No file part in the request.', 'error')
        return redirect(url_for('backup_restore'))
    
    file = request.files['backup_file']
    if file.filename == '':
        flash('No file selected for uploading.', 'error')
        return redirect(url_for('backup_restore'))

    if file and file.filename.endswith('.json'):
        try:
            content = file.read().decode('utf-8')
            json.loads(content)
            with open(CONFIG_FILE, 'w') as f:
                f.write(content)
            flash('Configuration restored successfully!', 'success')
        except Exception as e:
            flash(f'Error processing file: {e}', 'error')
    else:
        flash('Invalid file type. Please upload a .json file.', 'error')

    return redirect(url_for('backup_restore'))


if __name__ == "__main__":
    print("This script is for the web UI and is not meant to be run directly for production.")
    print("Use Gunicorn to serve the 'app' object in this file.")
    print("Example: gunicorn --bind 0.0.0.0:5000 main_web:app")
    app.run(host='0.0.0.0', port=5000, debug=True)
