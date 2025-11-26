# Hot Off The PRSS
A pair of Python files to fetch RSS feeds and send them to a Discord channel via webhooks. No limit on feeds, channels, or servers. Easy to self host at home or on a remote server. Links sent to Discord are Rich Embeds so they look nice.

<img src="https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/dark.png?raw=true">
<img src="https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/light.png?raw=true">

# Features

### Comprehensive Web Interface

- Modern Dashboard: A sleek, responsive web UI built with Tailwind CSS, featuring a dark/light mode toggle for comfortable viewing in any environment.

- Complete Feed Management: Add, edit, pause, force-check, and delete feeds directly from your browser.

- Secure Admin Login: The app prompts you to create a secure Owner account on first run. All passwords are automatically salted and hashed using scrypt.

### Advanced Role-Based Access Control (RBAC)

- Owner: Full control over the entire system. Can manage feeds, create/delete users, reset any password, promote/demote users, and access full system backups.

- Super Admin: Can manage feeds and perform user management for standard Admins (create, delete, reset password). Cannot modify the Owner or other Super Admins.

- Admin: Can add, edit, and manage RSS feeds but has no access to user management or sensitive system backups.

### Powerful Feed Configuration

- Multi-Webhook Destinations: Send a single RSS feed to multiple Discord channels or servers simultaneously. Each destination gets its own custom label (e.g., "Gaming Server - #news").

- Pause & Resume: Temporarily stop a feed from checking without deleting it. Perfect for maintenance or quieting a spammy source.

- Force Check: Instantly trigger a feed check from the dashboard to verify new configurations or test connectivity.

- Rich Embeds: Articles are posted as clean, professional Discord embeds rather than plain text links.

- Custom Intervals: Set a unique update interval (in seconds) for each individual feed.

### Intelligent & Reliable Fetching

- Per-Webhook Memory: The Script tracks sent articles individually for each webhook. This ensures that if you add a new destination to an existing feed, the new channel will receive posts without causing duplicates in the old ones.

- 24-Hour Rolling Window: The Script only considers articles published within the last 24 hours, preventing accidental floods of old content.

- Smart Initial Check: When a new feed is added, the Script silently seeds its memory with all recent articles. It will only post new content published after that moment, preventing an initial spam storm.

- Atomic State Management: Uses robust file locking and atomic write operations to prevent race conditions and duplicate posts, even under high load.

### Monitoring & Administration
- Live Status Badges: Dashboard shows color-coded status badges for every feed (Green for OK, Red for Error, Grey for Paused).

- Detailed Stats: At-a-glance cards show total feeds, active vs. paused webhooks, and error counts.

-  Full Backup & Restore:
   Config Backup: Download/restore your config.json to save your feed list.
   User DB Backup: (Owner only) Download/restore the encrypted user.json database to migrate accounts and passwords safely.

### Stable & Efficient Architecture

- Webhook-Based: Uses Discord webhooks for posting, eliminating the need for Script tokens and complex permissions.

- Separated Services: The web UI and the background scheduler run as two separate, independent processes for maximum stability.

- Docker Ready: Fully containerized with a production-ready Dockerfile and docker-compose.yml, using Gunicorn for the web server.

- Lightweight: Built with Python and Flask, designed to run efficiently on low-power hardware like a Raspberry Pi or a small VPS.

# Requirements (DOCKER STEPS SOON)

- A Linux server (e.g., Debian, Ubuntu) or a local machine for hosting. Also should work on Windows and MacOS but I have no way of testing there.

- Python 3.8+

- pip and venv for managing Python packages.

# Setup & Installation
Follow these steps to get your RSS Script up and running on a Debian-based server.

## 1. Clone this repository to a directory on your server.
```
git clone https://github.com/ReverendRetro/SimpleDiscordRSS.git
```

Ensure you have the following files in your project directory (e.g., /home/your_user/discord-rss-Script): <br>
main_web.py (The web interface) <br>
scheduler.py (The background feed checker)

## 2. Set Up Python Environment
Create a virtual environment to keep the project's dependencies isolated.

### Navigate to your project directory
```
cd /path/to/your/discord-rss-Script
```

### Ensure needed deps are installed
```
sudo apt install python3 python3-venv python3-pip -y
```

### Create the virtual environment
```
python3 -m venv venv
```

### Activate it
```
source venv/bin/activate
```


## 3. Install Dependencies
Create a requirements.txt file:
```
nano requirements.txt
```

# Add the following lines to the file:
```
feedparser
PyYAML
Flask
gunicorn
requests
werkzeug
```

Save the file (Ctrl+X, Y, Enter) and then install the packages:
```
pip install -r requirements.txt
```


## 4. Get a Discord Webhook URL
You'll need a webhook URL for each channel you want to post to.
- In your Discord server, go to the channel settings (click the ⚙️ icon).
- Navigate to the Integrations tab.
- Click "Create Webhook".
- Give the webhook a name (e.g., "RSS Feeds") and copy the Webhook URL.


## 5. Running the Script as a Service (Recommended)
To ensure the Script runs 24/7 and restarts automatically, we will set up two separate systemd services: one for the web UI and one for the scheduler.
## 1. Create the Web UI Service
Create a service file for the Gunicorn web server.
```
sudo nano /etc/systemd/system/discord-rss-web.service
```


Paste the following configuration. Remember to replace your_user with your actual Linux username and update the paths if necessary.
```
[Unit]
Description=Gunicorn instance to serve Discord RSS Script Web UI
After=network.target

[Service]
User=your_user
Group=your_user
WorkingDirectory=/home/your_user/discord-rss-Script
Environment="PATH=/home/your_user/discord-rss-Script/venv/bin"
ExecStart=/home/your_user/discord-rss-Script/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:5000 "main_web:app"
Restart=always

[Install]
WantedBy=multi-user.target
```

## 6. Create the Scheduler Service
Create a second service file for the background scheduler.
```
sudo nano /etc/systemd/system/discord-rss-scheduler.service
```


Paste the following configuration, again replacing your_user and the paths.
```
[Unit]
Description=Scheduler for Discord RSS Script
After=network.target

[Service]
User=your_user
Group=your_user
WorkingDirectory=/home/your_user/discord-rss-Script
ExecStart=/home/your_user/discord-rss-Script/venv/bin/python scheduler.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## 7. Enable and Start the Services
Now, tell systemd to recognize, enable, and start your new services.
### Reload systemd to recognize the new service files
```
sudo systemctl daemon-reload
```

### Enable and start the web UI service
```
sudo systemctl enable discord-rss-web.service
```
```
sudo systemctl start discord-rss-web.service
```

### Enable and start the scheduler service
```
sudo systemctl enable discord-rss-scheduler.service
```
```
sudo systemctl start discord-rss-scheduler.service
```


### Check the Status
You can check the status of each service independently:
```
sudo systemctl status discord-rss-web.service
```
```
sudo systemctl status discord-rss-scheduler.service
```

Check for errors:
```
sudo journalctl -u discord-rss-scheduler -n 50 --no-pager
```
```
sudo journalctl -u discord-rss-web -n 50 --no-pager
```


The scheduler log should show "Scheduler started." and "Scheduler running check..." messages.

## 8. Usage
Once Scripth services are running, you can manage the Script entirely through its web interface.

### 1. First-Time Setup & Login

Create Admin Account: Navigate to http://<your_server_ip>:5000 in your web browser. On your first visit, you will be prompted to create a secure admin account with a username and password.

Login: After creating the account, you will be taken to the login page. Use your new credentials to log into the control panel. On all future visits, you will be required to log in to access the dashboard.

### 2. Viewing Your Feeds

The main page provides a complete overview of all your configured RSS feeds.

Feed Status: Shows the live HTTP status code from the last time the feed was checked. It's color-coded for easy diagnosis:

Green (2xx): The feed is healthy and accessible.

Yellow (3xx): The feed has been redirected. It's working, but you may want to update the URL.

Red (4xx/5xx): There is an error. The feed might be broken (404 Not Found) or the server might be down.

Last Post Status: Displays the result of the last attempt to post an article, along with a relative timestamp (e.g., "about a minute ago").

RSS Name: The custom name you've given the feed for easy identification.

Webhook Destinations: Shows the custom labels you've assigned to each webhook URL, so you know exactly which servers and channels the feed is posting to.

### 3. Adding and Editing Feeds

Add a New Feed:

Click the "Add New Feed" button.

Fill in the form:

RSS Name: A friendly name for the feed (e.g., "Cybersecurity News").

RSS Feed URL: The direct URL of the RSS/Atom feed.

Discord Webhook Destinations: Click the "+" button to add one or more webhook rows. For each row, provide:

The Webhook URL you copied from your Discord channel.

An optional Label to describe the destination (e.g., "Tech Server - #alerts").

Refresh Interval: How often (in seconds) the Script should check for new articles.

Edit a Feed: Click the "Edit" link next to any feed to modify its settings, including adding or removing webhook destinations.

### 4. Backup and Restore

Navigate to the "Backup / Restore" page.

Download Backup: Click the button to save a complete config.json file of all your current feeds.

Restore from Backup: Upload a previously saved config.json file to instantly restore your configuration. This will overwrite your existing feeds.

# Configuration Files
The Script automatically creates and manages the configuration files in the directory it is created. No manual input required.
