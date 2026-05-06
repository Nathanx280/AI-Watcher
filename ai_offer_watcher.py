import json
import queue
import re
import threading
import time
import webbrowser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tkinter import (
    Tk,
    Toplevel,
    StringVar,
    BooleanVar,
    IntVar,
    END,
    BOTH,
    LEFT,
    RIGHT,
    X,
    Y,
    YES,
    Frame,
    Label,
    Entry,
    Button,
    Text,
    Checkbutton,
    Listbox,
    Scrollbar,
    SINGLE,
    messagebox,
)
from tkinter import ttk
from typing import Optional, List, Dict, Any

import requests
import feedparser

# Optional desktop notifications
try:
    from plyer import notification
    PLYER_AVAILABLE = True
except Exception:
    PLYER_AVAILABLE = False

# Optional system tray support
try:
    import pystray
    from pystray import MenuItem as TrayMenuItem, Menu as TrayMenu
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except Exception:
    PYSTRAY_AVAILABLE = False


APP_NAME = "AI Offer Watcher"
APP_DIR = Path.home() / ".ai_offer_watcher"
CONFIG_FILE = APP_DIR / "config.json"
SEEN_FILE = APP_DIR / "seen_items.json"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "watcher.log"

DEFAULT_CONFIG = {
    "check_interval_minutes": 15,
    "desktop_notifications": True,
    "popup_notifications": False,  # safer default
    "auto_start_checking": True,
    "minimize_to_tray_on_close": True,
    "max_items_per_feed": 10,
    "request_timeout_seconds": 15,
    "alert_cooldown_seconds": 60,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Offer-Watcher/1.1",
    "keywords": [
        "free ai",
        "free credits",
        "free api credits",
        "free trial",
        "ai credits",
        "gemini free",
        "google ai",
        "google developers",
        "google for developers",
        "google ai studio",
        "vertex ai credits",
        "developer offer",
        "startup credits",
        "hackathon credits",
        "openai credits",
        "anthropic credits",
        "free llm",
        "free inference",
        "free token credits",
    ],
    "feeds": [
        {
            "name": "Google for Developers Blog",
            "url": "https://developers.googleblog.com/feeds/posts/default?alt=rss",
            "enabled": True
        },
        {
            "name": "Google AI Blog",
            "url": "https://blog.google/technology/ai/rss/",
            "enabled": True
        },
        {
            "name": "Google News - Free AI Offers",
            "url": "https://news.google.com/rss/search?q=%22free%20AI%22%20OR%20%22AI%20credits%22%20OR%20%22free%20AI%20API%22&hl=en-AU&gl=AU&ceid=AU:en",
            "enabled": True
        },
        {
            "name": "Google News - Google Developers AI",
            "url": "https://news.google.com/rss/search?q=%22Google%20for%20Developers%22%20AI%20OR%20%22Google%20AI%20Studio%22%20OR%20Gemini%20developers&hl=en-AU&gl=AU&ceid=AU:en",
            "enabled": True
        },
        {
            "name": "Google News - Free API Credits AI",
            "url": "https://news.google.com/rss/search?q=%22free%20API%20credits%22%20AI%20OR%20%22developer%20credits%22%20AI&hl=en-AU&gl=AU&ceid=AU:en",
            "enabled": True
        },
    ]
}


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def now_local_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    ensure_app_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_log(message: str) -> None:
    ensure_app_dir()
    line = f"[{now_local_str()}] {message}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_date(entry: Dict[str, Any]) -> str:
    candidates = [
        entry.get("published"),
        entry.get("updated"),
        entry.get("pubDate"),
    ]
    for value in candidates:
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return ""


class WatcherEngine:
    def __init__(self, ui_callback_log, ui_callback_new_items, ui_callback_status):
        self.ui_callback_log = ui_callback_log
        self.ui_callback_new_items = ui_callback_new_items
        self.ui_callback_status = ui_callback_status

        ensure_app_dir()
        self.config = load_json(CONFIG_FILE, DEFAULT_CONFIG.copy())
        self.seen = load_json(SEEN_FILE, {"items": []})
        self.state = load_json(STATE_FILE, {
            "baseline_built": False,
            "last_alert_time": 0.0,
        })

        self._seen_set = set(self.seen.get("items", []))
        self._stop_event = threading.Event()
        self._thread = None
        self._is_running = False
        self._lock = threading.Lock()

    def save(self) -> None:
        save_json(CONFIG_FILE, self.config)
        save_json(SEEN_FILE, {"items": sorted(list(self._seen_set))})
        save_json(STATE_FILE, self.state)

    def is_running(self) -> bool:
        return self._is_running

    def log(self, message: str) -> None:
        append_log(message)
        self.ui_callback_log(message)

    def status(self, message: str) -> None:
        self.ui_callback_status(message)

    def set_config(self, new_config: dict) -> None:
        with self._lock:
            self.config = new_config
            self.save()

    def get_config(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.config))

    def mark_seen(self, item_id: str) -> None:
        self._seen_set.add(item_id)

    def already_seen(self, item_id: str) -> bool:
        return item_id in self._seen_set

    def build_item_id(self, item: dict) -> str:
        return f"{item.get('feed_name', '')}|{item.get('title', '')}|{item.get('link', '')}"

    def matches_keywords(self, text: str, keywords: List[str]) -> bool:
        hay = normalize_text(text)
        return any(normalize_text(k) in hay for k in keywords if k.strip())

    def fetch_feed(
        self,
        feed_def: dict,
        keywords: List[str],
        timeout: int,
        user_agent: str,
        max_items: int
    ) -> List[dict]:
        headers = {"User-Agent": user_agent}
        url = feed_def["url"]
        name = feed_def["name"]

        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            parsed = feedparser.parse(response.text)
        except Exception as exc:
            self.log(f"Feed error [{name}]: {exc}")
            return []

        matches = []
        entries = parsed.entries[:max_items]

        for entry in entries:
            title = (entry.get("title") or "").strip()
            summary = entry.get("summary", "") or entry.get("description", "") or ""
            link = (entry.get("link") or "").strip()
            published = parse_date(entry)

            combined = f"{title}\n{summary}\n{link}\n{name}"
            if self.matches_keywords(combined, keywords):
                item = {
                    "feed_name": name,
                    "title": title or "(no title)",
                    "summary": strip_html(summary),
                    "link": link,
                    "published": published,
                }
                matches.append(item)

        return matches

    def check_once(self) -> List[dict]:
        config = self.get_config()
        keywords = config.get("keywords", [])
        timeout = int(config.get("request_timeout_seconds", 15))
        user_agent = config.get("user_agent", DEFAULT_CONFIG["user_agent"])
        max_items = int(config.get("max_items_per_feed", 10))

        feeds = [f for f in config.get("feeds", []) if f.get("enabled", True)]
        self.log(f"Checking {len(feeds)} feed(s)...")

        found_items = []
        brand_new_items = []

        for feed_def in feeds:
            items = self.fetch_feed(feed_def, keywords, timeout, user_agent, max_items)
            for item in items:
                item_id = self.build_item_id(item)
                found_items.append((item_id, item))
                if not self.already_seen(item_id):
                    brand_new_items.append((item_id, item))

        # First run creates baseline only
        if not self.state.get("baseline_built", False):
            for item_id, _item in found_items:
                self.mark_seen(item_id)
            self.state["baseline_built"] = True
            self.save()
            self.log(f"Baseline created from {len(found_items)} matching item(s). No alerts sent.")
            return []

        new_items = []
        for item_id, item in brand_new_items:
            self.mark_seen(item_id)
            new_items.append(item)

        self.save()

        if new_items:
            self.log(f"Found {len(new_items)} new matching item(s).")
            self.ui_callback_new_items(new_items)
        else:
            self.log("No new matching items found.")

        return new_items

    def can_alert_now(self) -> bool:
        cooldown = max(10, int(self.config.get("alert_cooldown_seconds", 60)))
        last_alert = float(self.state.get("last_alert_time", 0.0))
        return (time.time() - last_alert) >= cooldown

    def mark_alert_sent(self) -> None:
        self.state["last_alert_time"] = time.time()
        self.save()

    def _run_loop(self) -> None:
        self._is_running = True
        self.status("Running")
        self.log("Background watcher started.")

        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:
                self.log(f"Watcher loop error: {exc}")

            interval_minutes = max(1, int(self.get_config().get("check_interval_minutes", 15)))
            sleep_seconds = interval_minutes * 60

            for _ in range(sleep_seconds):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        self._is_running = False
        self.status("Stopped")
        self.log("Background watcher stopped.")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.log("Watcher is already running.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def reset_seen(self) -> None:
        self._seen_set.clear()
        self.state["baseline_built"] = False
        self.state["last_alert_time"] = 0.0
        self.save()
        self.log("Seen history and baseline cleared.")


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1180x760")
        self.root.minsize(1050, 680)

        self.ui_queue = queue.Queue()
        self.engine = WatcherEngine(
            ui_callback_log=self.enqueue_log,
            ui_callback_new_items=self.enqueue_new_items,
            ui_callback_status=self.enqueue_status,
        )

        self.tray_icon = None
        self.hidden_to_tray = False

        self.status_var = StringVar(value="Stopped")
        self.interval_var = IntVar(value=self.engine.config.get("check_interval_minutes", 15))
        self.desktop_notify_var = BooleanVar(value=self.engine.config.get("desktop_notifications", True))
        self.popup_notify_var = BooleanVar(value=self.engine.config.get("popup_notifications", False))
        self.auto_start_var = BooleanVar(value=self.engine.config.get("auto_start_checking", True))
        self.tray_close_var = BooleanVar(value=self.engine.config.get("minimize_to_tray_on_close", True))
        self.timeout_var = IntVar(value=self.engine.config.get("request_timeout_seconds", 15))
        self.max_items_var = IntVar(value=self.engine.config.get("max_items_per_feed", 10))
        self.cooldown_var = IntVar(value=self.engine.config.get("alert_cooldown_seconds", 60))

        self.keyword_entry_var = StringVar()

        self.build_ui()
        self.populate_from_config()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(300, self.process_ui_queue)

        if self.auto_start_var.get():
            self.start_watcher()

    def build_ui(self) -> None:
        top = Frame(self.root)
        top.pack(fill=X, padx=10, pady=10)

        status_frame = Frame(top)
        status_frame.pack(side=LEFT, fill=X, expand=True)

        Label(status_frame, text="Status:", font=("Segoe UI", 10, "bold")).pack(side=LEFT)
        self.status_label = Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 10))
        self.status_label.pack(side=LEFT, padx=(6, 0))

        btn_frame = Frame(top)
        btn_frame.pack(side=RIGHT)

        Button(btn_frame, text="Start", width=12, command=self.start_watcher).pack(side=LEFT, padx=4)
        Button(btn_frame, text="Stop", width=12, command=self.stop_watcher).pack(side=LEFT, padx=4)
        Button(btn_frame, text="Check Now", width=12, command=self.check_now).pack(side=LEFT, padx=4)
        Button(btn_frame, text="Save Settings", width=14, command=self.save_settings).pack(side=LEFT, padx=4)
        Button(btn_frame, text="Reset Seen", width=12, command=self.reset_seen).pack(side=LEFT, padx=4)

        main = Frame(self.root)
        main.pack(fill=BOTH, expand=YES, padx=10, pady=(0, 10))

        left = Frame(main)
        left.pack(side=LEFT, fill=BOTH, expand=YES)

        right = Frame(main, width=390)
        right.pack(side=RIGHT, fill=Y, padx=(10, 0))

        settings_card = ttk.LabelFrame(left, text="Settings")
        settings_card.pack(fill=X, pady=(0, 10))

        row1 = Frame(settings_card)
        row1.pack(fill=X, padx=10, pady=8)

        Label(row1, text="Check every (minutes):").pack(side=LEFT)
        Entry(row1, textvariable=self.interval_var, width=8).pack(side=LEFT, padx=(8, 16))

        Label(row1, text="Request timeout (sec):").pack(side=LEFT)
        Entry(row1, textvariable=self.timeout_var, width=8).pack(side=LEFT, padx=(8, 16))

        Label(row1, text="Max items/feed:").pack(side=LEFT)
        Entry(row1, textvariable=self.max_items_var, width=8).pack(side=LEFT, padx=(8, 16))

        Label(row1, text="Alert cooldown (sec):").pack(side=LEFT)
        Entry(row1, textvariable=self.cooldown_var, width=8).pack(side=LEFT, padx=(8, 16))

        row2 = Frame(settings_card)
        row2.pack(fill=X, padx=10, pady=(0, 8))

        Checkbutton(row2, text="Desktop notifications", variable=self.desktop_notify_var).pack(side=LEFT, padx=(0, 12))
        Checkbutton(row2, text="Popup notifications", variable=self.popup_notify_var).pack(side=LEFT, padx=(0, 12))
        Checkbutton(row2, text="Auto-start checking", variable=self.auto_start_var).pack(side=LEFT, padx=(0, 12))
        Checkbutton(row2, text="Minimize to tray on close", variable=self.tray_close_var).pack(side=LEFT)

        keyword_card = ttk.LabelFrame(left, text="Keyword Filters")
        keyword_card.pack(fill=BOTH, expand=False, pady=(0, 10))

        keyword_top = Frame(keyword_card)
        keyword_top.pack(fill=X, padx=10, pady=8)

        Entry(keyword_top, textvariable=self.keyword_entry_var).pack(side=LEFT, fill=X, expand=YES, padx=(0, 8))
        Button(keyword_top, text="Add Keyword", command=self.add_keyword).pack(side=LEFT, padx=(0, 6))
        Button(keyword_top, text="Remove Selected", command=self.remove_selected_keyword).pack(side=LEFT)

        keyword_list_frame = Frame(keyword_card)
        keyword_list_frame.pack(fill=BOTH, expand=YES, padx=10, pady=(0, 10))

        self.keyword_listbox = Listbox(keyword_list_frame, selectmode=SINGLE, height=10)
        self.keyword_listbox.pack(side=LEFT, fill=BOTH, expand=YES)

        keyword_scroll = Scrollbar(keyword_list_frame, orient="vertical", command=self.keyword_listbox.yview)
        keyword_scroll.pack(side=RIGHT, fill=Y)
        self.keyword_listbox.config(yscrollcommand=keyword_scroll.set)

        feed_card = ttk.LabelFrame(left, text="Feeds")
        feed_card.pack(fill=BOTH, expand=YES)

        feed_top = Frame(feed_card)
        feed_top.pack(fill=X, padx=10, pady=8)

        Button(feed_top, text="Add Feed", command=self.add_feed_dialog).pack(side=LEFT, padx=(0, 6))
        Button(feed_top, text="Edit Selected Feed", command=self.edit_selected_feed).pack(side=LEFT, padx=(0, 6))
        Button(feed_top, text="Delete Selected Feed", command=self.delete_selected_feed).pack(side=LEFT)

        self.feed_tree = ttk.Treeview(feed_card, columns=("enabled", "name", "url"), show="headings", height=14)
        self.feed_tree.heading("enabled", text="Enabled")
        self.feed_tree.heading("name", text="Name")
        self.feed_tree.heading("url", text="URL")
        self.feed_tree.column("enabled", width=70, anchor="center")
        self.feed_tree.column("name", width=240)
        self.feed_tree.column("url", width=560)
        self.feed_tree.pack(fill=BOTH, expand=YES, padx=10, pady=(0, 10))

        new_items_card = ttk.LabelFrame(right, text="New Matches")
        new_items_card.pack(fill=BOTH, expand=YES, pady=(0, 10))

        self.results_tree = ttk.Treeview(new_items_card, columns=("time", "feed", "title"), show="headings", height=18)
        self.results_tree.heading("time", text="Published")
        self.results_tree.heading("feed", text="Feed")
        self.results_tree.heading("title", text="Title")
        self.results_tree.column("time", width=120)
        self.results_tree.column("feed", width=140)
        self.results_tree.column("title", width=540)
        self.results_tree.pack(fill=BOTH, expand=YES, padx=10, pady=(10, 6))
        self.results_tree.bind("<Double-1>", self.open_selected_result)

        result_btns = Frame(new_items_card)
        result_btns.pack(fill=X, padx=10, pady=(0, 10))
        Button(result_btns, text="Open Selected", command=self.open_selected_result).pack(side=LEFT, padx=(0, 6))
        Button(result_btns, text="Clear List", command=self.clear_results).pack(side=LEFT)

        log_card = ttk.LabelFrame(right, text="Log")
        log_card.pack(fill=BOTH, expand=YES)

        self.log_text = Text(log_card, wrap="word", height=14)
        self.log_text.pack(fill=BOTH, expand=YES, padx=10, pady=10)
        self.log_text.config(state="disabled")

        bottom = Frame(self.root)
        bottom.pack(fill=X, padx=10, pady=(0, 10))
        Label(
            bottom,
            text="First run builds a baseline silently. New alerts only appear for later matches."
        ).pack(side=LEFT)

    def populate_from_config(self) -> None:
        self.keyword_listbox.delete(0, END)
        for kw in self.engine.config.get("keywords", []):
            self.keyword_listbox.insert(END, kw)

        for row in self.feed_tree.get_children():
            self.feed_tree.delete(row)

        for idx, feed in enumerate(self.engine.config.get("feeds", [])):
            enabled = "Yes" if feed.get("enabled", True) else "No"
            self.feed_tree.insert("", END, iid=str(idx), values=(enabled, feed.get("name", ""), feed.get("url", "")))

    def save_settings(self) -> None:
        keywords = [self.keyword_listbox.get(i) for i in range(self.keyword_listbox.size())]
        feeds = []
        for idx in self.feed_tree.get_children():
            values = self.feed_tree.item(idx, "values")
            feeds.append({
                "enabled": values[0] == "Yes",
                "name": values[1],
                "url": values[2],
            })

        new_config = {
            "check_interval_minutes": max(1, int(self.interval_var.get())),
            "desktop_notifications": bool(self.desktop_notify_var.get()),
            "popup_notifications": bool(self.popup_notify_var.get()),
            "auto_start_checking": bool(self.auto_start_var.get()),
            "minimize_to_tray_on_close": bool(self.tray_close_var.get()),
            "max_items_per_feed": max(1, int(self.max_items_var.get())),
            "request_timeout_seconds": max(5, int(self.timeout_var.get())),
            "alert_cooldown_seconds": max(10, int(self.cooldown_var.get())),
            "user_agent": DEFAULT_CONFIG["user_agent"],
            "keywords": keywords,
            "feeds": feeds,
        }

        self.engine.set_config(new_config)
        self.append_log_ui("Settings saved.")

    def add_keyword(self) -> None:
        kw = self.keyword_entry_var.get().strip()
        if not kw:
            return
        existing = [self.keyword_listbox.get(i).lower() for i in range(self.keyword_listbox.size())]
        if kw.lower() in existing:
            messagebox.showinfo(APP_NAME, "That keyword already exists.")
            return
        self.keyword_listbox.insert(END, kw)
        self.keyword_entry_var.set("")

    def remove_selected_keyword(self) -> None:
        selection = self.keyword_listbox.curselection()
        if not selection:
            return
        self.keyword_listbox.delete(selection[0])

    def add_feed_dialog(self) -> None:
        self.feed_editor_dialog(title="Add Feed")

    def edit_selected_feed(self) -> None:
        selection = self.feed_tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select a feed first.")
            return

        item_id = selection[0]
        values = self.feed_tree.item(item_id, "values")
        feed = {
            "enabled": values[0] == "Yes",
            "name": values[1],
            "url": values[2],
        }
        self.feed_editor_dialog(title="Edit Feed", existing_item_id=item_id, existing_feed=feed)

    def delete_selected_feed(self) -> None:
        selection = self.feed_tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select a feed first.")
            return
        self.feed_tree.delete(selection[0])

    def feed_editor_dialog(
        self,
        title: str,
        existing_item_id: Optional[str] = None,
        existing_feed: Optional[dict] = None
    ) -> None:
        win = Toplevel(self.root)
        win.title(title)
        win.geometry("700x180")
        win.resizable(False, False)

        enabled_var = BooleanVar(value=(existing_feed or {}).get("enabled", True))
        name_var = StringVar(value=(existing_feed or {}).get("name", ""))
        url_var = StringVar(value=(existing_feed or {}).get("url", ""))

        row1 = Frame(win)
        row1.pack(fill=X, padx=12, pady=(12, 8))
        Checkbutton(row1, text="Enabled", variable=enabled_var).pack(side=LEFT)

        row2 = Frame(win)
        row2.pack(fill=X, padx=12, pady=8)
        Label(row2, text="Feed Name:", width=12, anchor="w").pack(side=LEFT)
        Entry(row2, textvariable=name_var).pack(side=LEFT, fill=X, expand=YES)

        row3 = Frame(win)
        row3.pack(fill=X, padx=12, pady=8)
        Label(row3, text="Feed URL:", width=12, anchor="w").pack(side=LEFT)
        Entry(row3, textvariable=url_var).pack(side=LEFT, fill=X, expand=YES)

        btns = Frame(win)
        btns.pack(fill=X, padx=12, pady=12)

        def save_feed():
            name = name_var.get().strip()
            url = url_var.get().strip()
            if not name or not url:
                messagebox.showerror(APP_NAME, "Name and URL are required.")
                return

            values = ("Yes" if enabled_var.get() else "No", name, url)

            if existing_item_id is None:
                next_id = str(len(self.feed_tree.get_children()))
                while next_id in self.feed_tree.get_children():
                    next_id = str(int(next_id) + 1)
                self.feed_tree.insert("", END, iid=next_id, values=values)
            else:
                self.feed_tree.item(existing_item_id, values=values)

            win.destroy()

        Button(btns, text="Save", width=12, command=save_feed).pack(side=LEFT, padx=(0, 6))
        Button(btns, text="Cancel", width=12, command=win.destroy).pack(side=LEFT)

    def start_watcher(self) -> None:
        self.save_settings()
        self.engine.start()
        self.status_var.set("Starting...")

    def stop_watcher(self) -> None:
        self.engine.stop()
        self.status_var.set("Stopping...")

    def check_now(self) -> None:
        self.save_settings()

        def runner():
            try:
                self.engine.status("Manual check")
                self.engine.check_once()
                self.engine.status("Running" if self.engine.is_running() else "Stopped")
            except Exception as exc:
                self.enqueue_log(f"Manual check failed: {exc}")

        threading.Thread(target=runner, daemon=True).start()

    def reset_seen(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Clear seen history and rebuild baseline on next check?"):
            return
        self.engine.reset_seen()

    def append_log_ui(self, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert(END, f"[{now_local_str()}] {message}\n")
        self.log_text.see(END)
        self.log_text.config(state="disabled")

    def enqueue_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def enqueue_status(self, message: str) -> None:
        self.ui_queue.put(("status", message))

    def enqueue_new_items(self, items: List[dict]) -> None:
        self.ui_queue.put(("new_items", items))

    def process_ui_queue(self) -> None:
        try:
            while True:
                action, payload = self.ui_queue.get_nowait()

                if action == "log":
                    self.append_log_ui(payload)

                elif action == "status":
                    self.status_var.set(payload)

                elif action == "new_items":
                    self.handle_new_items(payload)

        except queue.Empty:
            pass
        finally:
            self.root.after(300, self.process_ui_queue)

    def handle_new_items(self, items: List[dict]) -> None:
        for item in items:
            title = item.get("title", "")
            feed_name = item.get("feed_name", "")
            published = item.get("published", "")

            row_id = self.results_tree.insert("", 0, values=(published, feed_name, title))
            self.results_tree.item(row_id, tags=(json.dumps(item),))

        self.notify_user(items)

    def notify_user(self, items: List[dict]) -> None:
        if not items:
            return

        if not self.engine.can_alert_now():
            self.append_log_ui("Alert suppressed due to cooldown.")
            return

        self.engine.mark_alert_sent()
        cfg = self.engine.get_config()

        count = len(items)
        first = items[0]
        first_title = first.get("title", "New match")
        first_feed = first.get("feed_name", "")
        first_link = first.get("link", "")

        if count == 1:
            title = "1 new AI offer match"
            text = f"{first_feed}: {first_title[:160]}"
        else:
            title = f"{count} new AI offer matches"
            text = f"Latest: {first_feed}: {first_title[:140]}"

        if cfg.get("desktop_notifications", True) and PLYER_AVAILABLE:
            try:
                notification.notify(
                    title=title[:100],
                    message=text[:250],
                    app_name=APP_NAME,
                    timeout=10,
                )
            except Exception as exc:
                self.append_log_ui(f"Notification failed: {exc}")

        if cfg.get("popup_notifications", True):
            self.show_popup_alert(title, text, first_link)

    def show_popup_alert(self, title: str, text: str, link: str) -> None:
        pop = Toplevel(self.root)
        pop.title("New Match")
        pop.geometry("520x220")
        pop.attributes("-topmost", True)

        Label(pop, text=title, font=("Segoe UI", 11, "bold"), wraplength=480, justify=LEFT).pack(
            fill=X, padx=12, pady=(12, 8)
        )
        Label(pop, text=text or "New matching post found.", wraplength=480, justify=LEFT).pack(
            fill=X, padx=12, pady=(0, 12)
        )

        btns = Frame(pop)
        btns.pack(fill=X, padx=12, pady=(0, 12))

        def open_link():
            if link:
                webbrowser.open(link)
            pop.destroy()

        Button(btns, text="Open", width=12, command=open_link).pack(side=LEFT, padx=(0, 6))
        Button(btns, text="Dismiss", width=12, command=pop.destroy).pack(side=LEFT)

    def open_selected_result(self, event=None) -> None:
        selection = self.results_tree.selection()
        if not selection:
            return
        item_id = selection[0]
        tags = self.results_tree.item(item_id, "tags")
        if not tags:
            return
        try:
            item = json.loads(tags[0])
            link = item.get("link", "")
            if link:
                webbrowser.open(link)
        except Exception as exc:
            self.append_log_ui(f"Could not open result: {exc}")

    def clear_results(self) -> None:
        for row in self.results_tree.get_children():
            self.results_tree.delete(row)

    def on_close(self) -> None:
        cfg = self.engine.get_config()
        if cfg.get("minimize_to_tray_on_close", True) and PYSTRAY_AVAILABLE:
            self.hide_to_tray()
        else:
            self.shutdown()

    def create_tray_image(self):
        image = Image.new("RGB", (64, 64), color=(20, 20, 20))
        d = ImageDraw.Draw(image)
        d.rectangle((8, 8, 56, 56), outline=(0, 220, 255), width=3)
        d.ellipse((20, 20, 44, 44), outline=(0, 255, 140), width=3)
        return image

    def hide_to_tray(self) -> None:
        if not PYSTRAY_AVAILABLE:
            self.shutdown()
            return

        if self.hidden_to_tray:
            return

        self.hidden_to_tray = True
        self.root.withdraw()

        def show_window(icon=None, item=None):
            self.root.after(0, self.restore_from_tray)

        def quit_app(icon=None, item=None):
            self.root.after(0, self.shutdown)

        menu = TrayMenu(
            TrayMenuItem("Show", show_window),
            TrayMenuItem("Check Now", lambda icon, item: self.root.after(0, self.check_now)),
            TrayMenuItem("Quit", quit_app),
        )

        self.tray_icon = pystray.Icon(APP_NAME, self.create_tray_image(), APP_NAME, menu)

        def run_tray():
            try:
                self.tray_icon.run()
            except Exception as exc:
                self.append_log_ui(f"Tray error: {exc}")
                self.root.after(0, self.shutdown)

        threading.Thread(target=run_tray, daemon=True).start()
        self.append_log_ui("Minimized to tray.")

    def restore_from_tray(self) -> None:
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

        self.hidden_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def shutdown(self) -> None:
        try:
            self.engine.stop()
            self.engine.save()
        except Exception:
            pass

        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        self.root.destroy()


def main() -> None:
    ensure_app_dir()
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()