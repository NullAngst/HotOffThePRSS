# scheduler.py
# This script is a dedicated background process for fetching and posting RSS feeds.
# It's designed to be run as a standalone service.
#
# NEW LOGIC (as of 2025-11-14):
# - Implements per-webhook "sent" memory. sent_articles.yaml is now a dictionary
#   where each key is a webhook URL, and its value is a list of sent article IDs.

import os
import json
import yaml
import time
import uuid
import fcntl
import feedparser
import requests
from datetime import datetime, timedelta, timezone

# --- Get the absolute path of the script's directory ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Configuration & State Files (now with absolute paths) ---
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
SENT_ARTICLES_FILE = os.path.join(SCRIPT_DIR, "sent_articles.yaml")
FEED_STATE_FILE = os.path.join(SCRIPT_DIR, "feed_state.json")

# --- Set a common User-Agent for all requests ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/116.0"
feedparser.USER_AGENT = USER_AGENT

# --- Data Loading and Saving Functions ---

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"FEEDS": []}

def load_feed_state():
    try:
        with open(FEED_STATE_FILE, 'r') as f:
            content = f.read()
            if not content: return {}
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_feed_state(state_data):
    with open(FEED_STATE_FILE, 'w') as f:
        json.dump(state_data, f, indent=4)

def filter_and_update_sent_articles_for_webhook(webhook_url, article_ids_to_check):
    """
    Atomically checks which articles are new *for a specific webhook*
    and updates the sent articles file.
    Returns a set of article IDs that are genuinely new for this webhook.
    """
    new_article_ids = set()
    try:
        # Open the file for reading and writing ('r+')
        with open(SENT_ARTICLES_FILE, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            
            # Load the entire dictionary of all webhooks
            all_webhooks_memory = yaml.safe_load(f) or {}
            
            # Get the set of sent articles for *this specific webhook*
            sent_articles_list = all_webhooks_memory.get(webhook_url, [])
            sent_articles_set = set(sent_articles_list)
            
            # Determine which of the provided articles are new
            genuinely_new_ids = set(article_ids_to_check) - sent_articles_set
            
            if genuinely_new_ids:
                # Add the new IDs to this webhook's set
                updated_sent_articles_set = sent_articles_set.union(genuinely_new_ids)
                
                # Prune the list to the 10,000 most recent entries
                updated_sent_articles_list = sorted(list(updated_sent_articles_set))[-10000:]
                
                # Update the main dictionary with the new list for this webhook
                all_webhooks_memory[webhook_url] = updated_sent_articles_list
                
                # Go back to the beginning, clear it, and write the *entire* updated dictionary
                f.seek(0)
                f.truncate()
                yaml.dump(all_webhooks_memory, f)
                
                new_article_ids = genuinely_new_ids

            fcntl.flock(f, fcntl.LOCK_UN)
            
    except FileNotFoundError:
        # File doesn't exist, so all articles are new
        with open(SENT_ARTICLES_FILE, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            updated_sent_articles_list = sorted(list(article_ids_to_check))[-10000:]
            
            # Create the new dictionary structure
            all_webhooks_memory = {webhook_url: updated_sent_articles_list}
            
            yaml.dump(all_webhooks_memory, f)
            fcntl.flock(f, fcntl.LOCK_UN)
            
        new_article_ids = set(article_ids_to_check)
    except Exception as e:
        print(f"Error in filter_and_update_sent_articles_for_webhook: {e}")

    return new_article_ids

# --- Core Logic ---

def send_to_webhook(webhook_url, embed):
    """Sends a rich embed to a Discord webhook."""
    headers = {"Content-Type": "application/json"}
    payload = {"embeds": [embed]}
    try:
        response = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        if response.status_code in [200, 204]:
            return "Success"
        elif response.status_code == 429:
            print(f"Rate limited by Discord for webhook {webhook_url}.")
            return "Rate Limited"
        else:
            print(f"Error sending to webhook {webhook_url}: {response.status_code} - {response.text}")
            return f"Error: {response.status_code}"
    except requests.RequestException as e:
        print(f"Failed to connect to webhook {webhook_url}: {e}")
        return "Failed to Connect"

def check_single_feed(feed_config, feed_state):
    """Checks a single feed for new articles and posts them."""
    feed_url = feed_config.get("url")
    feed_id = feed_config.get("id")
    
    headers = {"User-Agent": USER_AGENT}
    feed_data = feedparser.parse(feed_url, request_headers=headers)
    
    status_code = feed_data.get('status', 500)
    last_post_status = None
    
    if not feed_data.entries:
        if status_code != 200:
            print(f"Could not fetch feed: {feed_url} (Status: {status_code})")
        return status_code, last_post_status

    now = datetime.now(timezone.utc)
    twenty_four_hours_ago = now - timedelta(hours=24)
    
    recent_articles = []
    for entry in feed_data.entries:
        # Check for published date, fallback to updated date (for Atom/GitHub)
        published_time = entry.get('published_parsed') or entry.get('updated_parsed')
        
        if published_time:
            try:
                published_dt = datetime.fromtimestamp(time.mktime(published_time)).replace(tzinfo=timezone.utc)
                if published_dt >= twenty_four_hours_ago:
                    recent_articles.append(entry)
            except Exception as e:
                print(f"Warning: Could not parse date for entry in {feed_url}: {e}")
                
    if not recent_articles:
        return status_code, last_post_status

    recent_articles.sort(key=lambda x: x.get('published_parsed') or x.get('updated_parsed') or (0,)*9, reverse=True)
    
    # Map of all recent article IDs (link or GUID) to their entry data
    article_id_map = {
        (entry.get('id') or entry.get('link')): entry
        for entry in recent_articles if (entry.get('id') or entry.get('link'))
    }
    all_recent_ids = list(article_id_map.keys())
    
    is_initial_check = feed_id not in feed_state
    
    webhooks = feed_config.get('webhooks', [])
    if not webhooks:
        return status_code, "No webhooks configured"

    if is_initial_check:
        print(f"Initial check for '{feed_url}'. Seeding memory for {len(webhooks)} webhook(s). No articles will be posted.")
        for webhook in webhooks:
            webhook_url = webhook.get("url")
            if webhook_url:
                # Silently seed the memory for this webhook
                filter_and_update_sent_articles_for_webhook(webhook_url, all_recent_ids)
        
        last_post_status = "Initial check (seeded)"
    else:
        # This is a normal, subsequent check.
        # Check each webhook independently.
        for webhook in webhooks:
            webhook_url = webhook.get("url")
            if not webhook_url:
                continue

            # Find new articles *for this specific webhook*
            newly_found_ids = filter_and_update_sent_articles_for_webhook(webhook_url, all_recent_ids)
            articles_to_post = [article_id_map[id] for id in newly_found_ids]
            
            if articles_to_post:
                # Sort them by date to post oldest-new first
                articles_to_post.sort(key=lambda x: x.get('published_parsed') or x.get('updated_parsed') or (0,)*9)
                print(f"Found {len(articles_to_post)} new article(s) for {feed_url} -> {webhook.get('label', webhook_url)}")
                
                for entry in articles_to_post:
                    title = entry.get('title', 'No Title')
                    link = entry.get('link', '')
                    summary = entry.get('summary', 'No summary available.')
                    
                    import re
                    summary = re.sub('<[^<]+?>', '', summary).strip()
                    if len(summary) > 250:
                        summary = summary[:247] + "..."

                    embed = {
                        "title": title, "url": link, "description": summary, "color": 5814783,
                        "footer": {"text": f"From: {feed_config.get('name', feed_url)}"}
                    }
                    
                    # Send to this specific webhook
                    last_post_status = send_to_webhook(webhook_url, embed)
    
    return status_code, last_post_status

# --- Main Scheduler Class ---

class FeedScheduler:
    def __init__(self, interval=60):
        self.interval = interval

    def run(self):
        print(f"Scheduler started. Using config file: {CONFIG_FILE}")
        while True:
            print("Scheduler running check...")
            config = load_config()
            feed_state = load_feed_state()
            now = datetime.now(timezone.utc)
            
            for feed_config in config.get("FEEDS", []):
                feed_id = feed_config.get("id")
                if not feed_id: continue
                
                # --- PAUSE CHECK ---
                # If active is explicitly False, skip this feed
                if not feed_config.get('active', True):
                    # Optional: Log that we are skipping?
                    # print(f"Skipping paused feed: {feed_config.get('name', feed_config.get('url'))}")
                    continue

                last_checked_str = feed_state.get(feed_id, {}).get('last_checked')
                update_interval = feed_config.get("update_interval", 300)

                should_check = True
                if last_checked_str:
                    try:
                        last_checked = datetime.fromisoformat(last_checked_str)
                        if now - last_checked < timedelta(seconds=update_interval):
                            should_check = False
                    except ValueError:
                        print(f"Warning: Invalid date format for last_checked for feed {feed_id}. Checking anyway.")
                
                if should_check:
                    print(f"Checking feed: {feed_config.get('url')}")
                    
                    try:
                        # Pass the current feed_state to the check function
                        status_code, last_post_status = check_single_feed(feed_config, feed_state)
                        
                        # Update feed state
                        if feed_id not in feed_state:
                            feed_state[feed_id] = {}
                            
                        feed_state[feed_id]['status_code'] = status_code
                        feed_state[feed_id]['last_checked'] = now.isoformat()
                        
                        if last_post_status:
                            feed_state[feed_id]['last_post'] = {
                                "status": last_post_status,
                                "timestamp": now.isoformat()
                            }
                        
                        save_feed_state(feed_state)
                    except Exception as e:
                        print(f"An unexpected error occurred while checking feed {feed_config.get('url')}: {e}")
                    
                    time.sleep(2)
            
            time.sleep(self.interval)

if __name__ == "__main__":
    scheduler = FeedScheduler()
    scheduler.run()
