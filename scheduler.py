# scheduler.py
# This script is a dedicated background process for fetching and posting RSS feeds.
# It's designed to be run as a standalone service.

import os
import json
import yaml
import time
import uuid
import feedparser
import requests
from datetime import datetime, timedelta, timezone

# --- Set a common User-Agent for all requests ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/116.0"
feedparser.USER_AGENT = USER_AGENT

# --- Configuration & State Files ---
CONFIG_FILE = "config.json"
SENT_ARTICLES_FILE = "sent_articles.yaml"
FEED_STATE_FILE = "feed_state.json"

# --- Data Loading and Saving Functions ---

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"FEEDS": []}

def load_sent_articles():
    try:
        with open(SENT_ARTICLES_FILE, 'r') as f:
            return set(yaml.safe_load(f) or [])
    except FileNotFoundError:
        return set()

def save_sent_articles(sent_ids):
    # Prune the list to the 10,000 most recent entries to prevent infinite growth
    with open(SENT_ARTICLES_FILE, 'w') as f:
        yaml.dump(list(sent_ids)[-10000:], f)

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
            print(f"Rate limited by Discord for webhook {webhook_url}. Retrying...")
            return "Rate Limited"
        else:
            print(f"Error sending to webhook {webhook_url}: {response.status_code} - {response.text}")
            return f"Error: {response.status_code}"
    except requests.RequestException as e:
        print(f"Failed to connect to webhook {webhook_url}: {e}")
        return "Failed to Connect"

def check_single_feed(feed_config, sent_articles):
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
        return [], status_code, last_post_status

    now = datetime.now(timezone.utc)
    twenty_four_hours_ago = now - timedelta(hours=24)
    
    # Filter articles published within the last 24 hours
    recent_articles = []
    for entry in feed_data.entries:
        published_time = entry.get('published_parsed')
        if published_time:
            published_dt = datetime.fromtimestamp(time.mktime(published_time)).replace(tzinfo=timezone.utc)
            if published_dt >= twenty_four_hours_ago:
                recent_articles.append(entry)

    if not recent_articles:
        return [], status_code, last_post_status

    # Sort to ensure the newest is first
    recent_articles.sort(key=lambda x: x.get('published_parsed', (0,)*9), reverse=True)
    
    # Check if this feed has ever been checked before
    feed_state = load_feed_state()
    is_initial_check = feed_id not in feed_state
    
    new_articles_to_post = []
    if is_initial_check:
        # On first check, only post the single newest article
        newest_article = recent_articles[0]
        new_articles_to_post.append(newest_article)
        
        # Then, silently add all other recent articles to memory to prevent them from being posted
        for article in recent_articles:
            article_id = article.get('id') or article.get('link')
            if article_id:
                sent_articles.add(article_id)
        print(f"Initial check for '{feed_url}'. Seeding memory with {len(recent_articles)} articles and posting 1.")
    else:
        # On subsequent checks, find all articles not in memory
        for entry in recent_articles:
            article_id = entry.get('id') or entry.get('link')
            if article_id and article_id not in sent_articles:
                new_articles_to_post.append(entry)

    # Post the new articles
    posted_ids = []
    for entry in reversed(new_articles_to_post): # Post oldest first
        article_id = entry.get('id') or entry.get('link')
        if not article_id: continue
        
        title = entry.get('title', 'No Title')
        link = entry.get('link', '')
        summary = entry.get('summary', 'No summary available.')
        
        # Clean up summary (basic HTML tag removal)
        import re
        summary = re.sub('<[^<]+?>', '', summary)
        summary = summary.strip()
        if len(summary) > 250:
            summary = summary[:247] + "..."

        embed = {
            "title": title,
            "url": link,
            "description": summary,
            "color": 5814783, # A nice blue color
            "footer": {"text": f"From: {feed_config.get('name', feed_url)}"}
        }

        # Handle different webhook structures for backward compatibility
        webhooks = feed_config.get('webhooks', [])
        if not webhooks and 'webhook_url' in feed_config: # Legacy single URL
            webhooks = [{"url": feed_config['webhook_url'], "label": ""}]
        
        for webhook in webhooks:
            webhook_url = webhook.get("url")
            if webhook_url:
                post_status = send_to_webhook(webhook_url, embed)
                # Keep track of the status of the last attempt
                last_post_status = post_status

        # Mark as posted only after attempting all webhooks for it
        posted_ids.append(article_id)

    return posted_ids, status_code, last_post_status

# --- Main Scheduler Class ---

class FeedScheduler:
    def __init__(self, interval=60):
        self.interval = interval

    def run(self):
        print("Scheduler started.")
        while True:
            print("Scheduler running check...")
            config = load_config()
            sent_articles = load_sent_articles()
            feed_state = load_feed_state()
            now = datetime.now(timezone.utc)
            state_changed = False
            
            for feed_config in config.get("FEEDS", []):
                feed_id = feed_config.get("id")
                if not feed_id: continue

                last_checked_str = feed_state.get(feed_id, {}).get('last_checked')
                update_interval = feed_config.get("update_interval", 300)

                should_check = True
                if last_checked_str:
                    last_checked = datetime.fromisoformat(last_checked_str)
                    if now - last_checked < timedelta(seconds=update_interval):
                        should_check = False
                
                if should_check:
                    print(f"Checking feed: {feed_config.get('url')}")
                    
                    # Ensure the state dict for this feed exists
                    if feed_id not in feed_state:
                        feed_state[feed_id] = {}

                    try:
                        newly_posted_ids, status_code, last_post_status = check_single_feed(feed_config, sent_articles)
                        
                        feed_state[feed_id]['status_code'] = status_code
                        feed_state[feed_id]['last_checked'] = now.isoformat()
                        
                        # If a post was attempted, record its status
                        if last_post_status:
                            feed_state[feed_id]['last_post'] = {
                                "status": last_post_status,
                                "timestamp": now.isoformat()
                            }

                        if newly_posted_ids:
                            sent_articles.update(newly_posted_ids)
                            save_sent_articles(sent_articles)
                        
                        state_changed = True
                    except Exception as e:
                        print(f"An unexpected error occurred while checking feed {feed_config.get('url')}: {e}")
                    
                    # Add a small delay between processing feeds to be gentler on resources
                    time.sleep(2) 
            
            if state_changed:
                save_feed_state(feed_state)

            time.sleep(self.interval)

if __name__ == "__main__":
    scheduler = FeedScheduler()
    scheduler.run()
