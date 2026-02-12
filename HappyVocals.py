import os
import sys
import threading
import time
import sqlite3
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import PyPDF2
import requests
from openai import OpenAI

# =========================
# Constants / DB
# =========================
APP_TITLE = "EduSummarizer Box"
DB_PATH = "edusummarizer.db"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TTS_MODEL = "eleven_monolingual_v1"

# =========================
# Database Helpers
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS voices(
            voice_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            added_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            pdf_path TEXT,
            style_prompt TEXT,
            voice_id TEXT,
            voice_name TEXT,
            summary_text TEXT,
            audio_path TEXT
        )
    """)
    conn.commit()
    conn.close()

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

def get_setting(key, default=""):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def upsert_voice(voice_id, name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO voices(voice_id,name,added_at) VALUES(?,?,?) ON CONFLICT(voice_id) DO UPDATE SET name=excluded.name",
                (voice_id, name, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

def insert_run(pdf_path, style_prompt, voice_id, voice_name, summary_text, audio_path):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""INSERT INTO runs(created_at,pdf_path,style_prompt,voice_id,voice_name,summary_text,audio_path)
                   VALUES(?,?,?,?,?,?,?)""",
                (datetime.now().isoformat(timespec="seconds"), pdf_path, style_prompt, voice_id, voice_name, summary_text, audio_path))
    conn.commit()
    conn.close()

# =========================
# Core Logic (same as your CLI, wrapped)
# =========================
def extract_pdf_text(file_path: str) -> str:
    all_text = ""
    with open(file_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page_num, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text()
            except Exception:
                text = None
            if text:
                all_text += f"\n--- Page {page_num} ---\n{text}\n"
            else:
                all_text += f"\n--- Page {page_num} ---\n[No extractable text]\n"
    return all_text

def summarize_pdf(openai_api_key: str, pdf_path: str, style_prompt: str) -> str:
    client = OpenAI(api_key=openai_api_key)
    pdf_text = extract_pdf_text(pdf_path)
    # Safety: truncate very long inputs to avoid token limit issues
    if len(pdf_text) > 180000:
        pdf_text = pdf_text[:180000] + "\n[Truncated due to size]"
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes documents. Only use the provided content; do not invent facts."},
            {"role": "user", "content": f"PDF content:\n{pdf_text}\n\nSummarize in this style: {style_prompt}\nReturn clean bullet points and short paragraphs suitable for audio narration."}
        ],
        temperature=0.3
    )
    return resp.choices[0].message.content.strip()

def eleven_list_voices(eleven_api_key: str):
    url = "https://api.elevenlabs.io/v1/voices"
    headers = {"xi-api-key": eleven_api_key}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()
    voices = data.get("voices", [])
    return [{"name": v["name"], "voice_id": v["voice_id"]} for v in voices]

def eleven_add_voice(eleven_api_key: str, voice_name: str, audio_file_path: str):
    url = "https://api.elevenlabs.io/v1/voices/add"
    headers = {"xi-api-key": eleven_api_key}
    with open(audio_file_path, "rb") as f:
        files = {"files": (os.path.basename(audio_file_path), f, "audio/mpeg")}
        data = {"name": voice_name, "description": "Instant cloned voice", "labels": '{"use_case":"personal"}'}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=300)
    if r.status_code == 200:
        return r.json()["voice_id"]
    raise RuntimeError(f"{r.status_code}: {r.text}")

def eleven_tts(eleven_api_key: str, voice_id: str, text: str, out_file: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": eleven_api_key, "Content-Type": "application/json"}
    data = {"text": text, "model_id": DEFAULT_TTS_MODEL}
    r = requests.post(url, headers=headers, json=data, timeout=300)
    if r.status_code == 200:
        with open(out_file, "wb") as f:
            f.write(r.content)
        return out_file
    raise RuntimeError(f"{r.status_code}: {r.text}")

# =========================
# UI
# =========================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x680")
        self.minsize(900, 640)

        # Style
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.configure(bg="#f7f7f9")
        self.style.configure("TFrame", background="#ffffff")
        self.style.configure("TLabel", background="#ffffff", foreground="#222", font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"))
        self.style.configure("TButton", font=("Segoe UI", 10))
        self.style.configure("TEntry", padding=4)
        self.style.configure("TCombobox", padding=4)

        # Vars
        self.pdf_path_var = tk.StringVar()
        self.style_prompt_var = tk.StringVar()
        self.openai_key_var = tk.StringVar(value=get_setting("OPENAI_API_KEY"))
        self.eleven_key_var = tk.StringVar(value=get_setting("ELEVEN_API_KEY"))
        self.voice_map = {}  # name -> id
        self.selected_voice_name = tk.StringVar()
        self.summary_text = tk.StringVar(value="")
        self.audio_out_path = None

        init_db()
        self._build_ui()

    # --------- UI Layout ----------
    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self, padding=16)
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(top, text=APP_TITLE, style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side=tk.RIGHT)
        ttk.Button(top, text="Refresh Voices", command=self.load_voices).pack(side=tk.RIGHT, padx=(0,8))
        ttk.Button(top, text="Add New Voice", command=self.open_add_voice).pack(side=tk.RIGHT, padx=(0,8))

        # Content area
        content = ttk.Frame(self, padding=(16, 8, 16, 16))
        content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        left = ttk.Frame(content, padding=12)
        left.pack(side=tk.LEFT, fill=tk.Y)
        right = ttk.Frame(content, padding=12)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Left: Inputs Card
        card = ttk.Frame(left, padding=12, relief="groove")
        card.pack(fill=tk.Y, expand=False)

        # PDF
        ttk.Label(card, text="PDF File").grid(row=0, column=0, sticky="w", pady=(2,2))
        file_row = ttk.Frame(card)
        file_row.grid(row=1, column=0, sticky="we", pady=(0,8))
        file_row.columnconfigure(0, weight=1)
        ttk.Entry(file_row, textvariable=self.pdf_path_var, width=36).grid(row=0, column=0, sticky="we")
        ttk.Button(file_row, text="Browse", command=self.pick_pdf).grid(row=0, column=1, padx=(6,0))

        # Style prompt
        ttk.Label(card, text="Summary Style Prompt").grid(row=2, column=0, sticky="w", pady=(6,2))
        self.style_entry = tk.Text(card, height=5, wrap="word", font=("Segoe UI", 10))
        self.style_entry.grid(row=3, column=0, sticky="we", pady=(0,8))
        self.style_entry.insert("1.0", "Exam oriented, concise bullet points with formulas preserved where present.")

        # Voice
        ttk.Label(card, text="Faculty Voice").grid(row=4, column=0, sticky="w", pady=(6,2))
        self.voice_combo = ttk.Combobox(card, textvariable=self.selected_voice_name, state="readonly")
        self.voice_combo.grid(row=5, column=0, sticky="we")
        ttk.Label(card, text="(Use 'Refresh Voices' after adding a new one)").grid(row=6, column=0, sticky="w", pady=(2,8))

        # Actions
        self.generate_btn = ttk.Button(card, text="Summarize → Generate Audio", command=self.handle_generate)
        self.generate_btn.grid(row=7, column=0, sticky="we", pady=(8,2))
        self.save_audio_btn = ttk.Button(card, text="Save/Play Last Audio", command=self.open_audio, state="disabled")
        self.save_audio_btn.grid(row=8, column=0, sticky="we", pady=(2,8))

        # Progress + status
        self.progress = ttk.Progressbar(card, mode="indeterminate")
        self.progress.grid(row=9, column=0, sticky="we", pady=(6,2))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(card, textvariable=self.status_var).grid(row=10, column=0, sticky="w", pady=(2,0))

        for i in range(0, 11):
            card.grid_rowconfigure(i, pad=2)
        card.grid_columnconfigure(0, weight=1)

        # Right: Summary Viewer
        summary_card = ttk.Frame(right, padding=12, relief="groove")
        summary_card.pack(fill=tk.BOTH, expand=True)
        ttk.Label(summary_card, text="Summarized Text", style="Header.TLabel").pack(anchor="w")

        self.summary_box = tk.Text(summary_card, wrap="word", font=("Segoe UI", 10), height=20)
        self.summary_box.pack(fill=tk.BOTH, expand=True, pady=(8,0))
        self.summary_box.configure(state="disabled")

        # History (optional quick view)
        hist_card = ttk.Frame(right, padding=12, relief="groove")
        hist_card.pack(fill=tk.BOTH, expand=False, pady=(12,0))
        ttk.Label(hist_card, text="Recent Runs", style="Header.TLabel").pack(anchor="w")
        self.history_tree = ttk.Treeview(hist_card, columns=("created","pdf","voice"), show="headings", height=6)
        self.history_tree.heading("created", text="Created At")
        self.history_tree.heading("pdf", text="PDF")
        self.history_tree.heading("voice", text="Voice")
        self.history_tree.column("created", width=160, anchor="w")
        self.history_tree.column("pdf", width=420, anchor="w")
        self.history_tree.column("voice", width=160, anchor="w")
        self.history_tree.pack(fill=tk.BOTH, expand=True, pady=(8,0))
        self.history_tree.bind("<Double-1>", self.load_run_from_history)

        # Init
        self.refresh_history()
        self.after(200, self.load_voices_auto)

    # --------- UI Handlers ----------
    def pick_pdf(self):
        path = filedialog.askopenfilename(title="Select PDF", filetypes=[("PDF files","*.pdf")])
        if path:
            self.pdf_path_var.set(path)

    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("520x240")
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="OpenAI API Key").grid(row=0, column=0, sticky="w")
        openai_entry = ttk.Entry(frm, textvariable=self.openai_key_var, show="•", width=56)
        openai_entry.grid(row=1, column=0, sticky="we", pady=(0,12))

        ttk.Label(frm, text="ElevenLabs API Key").grid(row=2, column=0, sticky="w")
        eleven_entry = ttk.Entry(frm, textvariable=self.eleven_key_var, show="•", width=56)
        eleven_entry.grid(row=3, column=0, sticky="we", pady=(0,12))

        def save_keys():
            set_setting("OPENAI_API_KEY", self.openai_key_var.get().strip())
            set_setting("ELEVEN_API_KEY", self.eleven_key_var.get().strip())
            messagebox.showinfo("Saved", "API keys saved securely in local SQLite DB.")
            win.destroy()

        ttk.Button(frm, text="Save", command=save_keys).grid(row=4, column=0, sticky="e")
        frm.grid_columnconfigure(0, weight=1)

    def open_add_voice(self):
        if not self.eleven_key_var.get().strip():
            messagebox.showwarning("Missing Key", "Please add your ElevenLabs API key in Settings first.")
            return
        win = tk.Toplevel(self)
        win.title("Add New Voice")
        win.geometry("520x240")
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)

        name_var = tk.StringVar()
        audio_var = tk.StringVar()

        ttk.Label(frm, text="Voice Name").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=name_var).grid(row=1, column=0, sticky="we", pady=(0,8))

        ttk.Label(frm, text="Voice Sample (mp3/wav)").grid(row=2, column=0, sticky="w")
        row = ttk.Frame(frm); row.grid(row=3, column=0, sticky="we")
        ttk.Entry(row, textvariable=audio_var).grid(row=0, column=0, sticky="we")
        ttk.Button(row, text="Browse", command=lambda: self._pick_audio(audio_var)).grid(row=0, column=1, padx=(8,0))

        status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status).grid(row=4, column=0, sticky="w", pady=(8,0))

        def do_add():
            vname = name_var.get().strip()
            apath = audio_var.get().strip()
            if not vname or not apath:
                messagebox.showwarning("Missing", "Please provide a name and audio file.")
                return
            self._run_bg(
                task=lambda: eleven_add_voice(self.eleven_key_var.get().strip(), vname, apath),
                on_start=lambda: status.set("Cloning voice... please wait."),
                on_success=lambda vid: self._after_add_voice(vid, vname, status, win),
                on_error=lambda e: status.set(f"Error: {e}")
            )

        ttk.Button(frm, text="Add Voice", command=do_add).grid(row=5, column=0, sticky="e", pady=(10,0))
        frm.grid_columnconfigure(0, weight=1)

    def _pick_audio(self, var):
        p = filedialog.askopenfilename(title="Select Audio Sample", filetypes=[("Audio","*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("All","*.*")])
        if p:
            var.set(p)

    def _after_add_voice(self, voice_id, name, status_var, win):
        upsert_voice(voice_id, name)
        status_var.set("Voice added successfully.")
        self.load_voices()
        self.selected_voice_name.set(name)
        messagebox.showinfo("Success", f"Voice '{name}' added.\nID: {voice_id}")
        win.destroy()

    def load_voices_auto(self):
        # Load from API if key present, else load from DB only
        if self.eleven_key_var.get().strip():
            self.load_voices()
        else:
            self._load_voices_from_db()

    def _load_voices_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT name, voice_id FROM voices ORDER BY name")
        rows = cur.fetchall()
        conn.close()
        self.voice_map = {name: vid for (name, vid) in rows}
        self.voice_combo["values"] = list(self.voice_map.keys())
        if self.voice_map and not self.selected_voice_name.get():
            self.selected_voice_name.set(list(self.voice_map.keys())[0])

    def load_voices(self):
        if not self.eleven_key_var.get().strip():
            self._load_voices_from_db()
            return

        def task():
            voices = eleven_list_voices(self.eleven_key_var.get().strip())
            # Persist to DB
            for v in voices:
                upsert_voice(v["voice_id"], v["name"])
            return voices

        def on_success(voices):
            self.voice_map = {v["name"]: v["voice_id"] for v in voices}
            self.voice_combo["values"] = list(self.voice_map.keys())
            if self.voice_map:
                self.selected_voice_name.set(list(self.voice_map.keys())[0])
            self.set_status("Voices loaded.")

        self._run_bg(task=task, on_start=lambda: self.set_status("Loading voices..."), on_success=on_success, on_error=lambda e: self.set_status(f"Voice load error: {e}"))

    def handle_generate(self):
        pdf_path = self.pdf_path_var.get().strip()
        if not pdf_path or not os.path.isfile(pdf_path):
            messagebox.showwarning("Missing PDF", "Please select a valid PDF file.")
            return
        style_prompt = self.style_entry.get("1.0", "end").strip()
        if not style_prompt:
            messagebox.showwarning("Missing Style", "Please enter a style prompt.")
            return
        if not self.openai_key_var.get().strip():
            messagebox.showwarning("Missing OpenAI Key", "Please add your OpenAI API key in Settings.")
            return
        if not self.eleven_key_var.get().strip():
            messagebox.showwarning("Missing ElevenLabs Key", "Please add your ElevenLabs API key in Settings.")
            return
        vname = self.selected_voice_name.get().strip()
        if not vname or vname not in self.voice_map:
            messagebox.showwarning("Voice", "Please select a faculty voice.")
            return
        voice_id = self.voice_map[vname]

        out_dir = Path.cwd() / "outputs"
        out_dir.mkdir(exist_ok=True)
        audio_out = out_dir / f"summary_{int(time.time())}.mp3"

        def task():
            summary = summarize_pdf(self.openai_key_var.get().strip(), pdf_path, style_prompt)
            eleven_tts(self.eleven_key_var.get().strip(), voice_id, summary, str(audio_out))
            return summary, str(audio_out)

        def on_success(result):
            summary, a_path = result
            self._set_summary_text(summary)
            self.audio_out_path = a_path
            self.save_audio_btn.configure(state="normal")
            insert_run(pdf_path, style_prompt, voice_id, vname, summary, a_path)
            self.refresh_history()
            self.set_status("Done. Audio generated.")
            messagebox.showinfo("Success", f"Summary generated and audio saved:\n{a_path}")

        self._run_bg(task=task, on_start=lambda: self.set_status("Summarizing and generating audio..."), on_success=on_success, on_error=lambda e: self.set_status(f"Error: {e}"))

    def open_audio(self):
        if not self.audio_out_path or not os.path.isfile(self.audio_out_path):
            messagebox.showinfo("No Audio", "No audio file available yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.audio_out_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{self.audio_out_path}"')
            else:
                os.system(f'xdg-open "{self.audio_out_path}"')
        except Exception as e:
            messagebox.showerror("Open Error", str(e))

    def refresh_history(self):
        for i in self.history_tree.get_children():
            self.history_tree.delete(i)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT created_at, pdf_path, voice_name FROM runs ORDER BY id DESC LIMIT 12")
        rows = cur.fetchall()
        conn.close()
        for r in rows:
            self.history_tree.insert("", tk.END, values=r)

    def load_run_from_history(self, _event):
        item = self.history_tree.focus()
        if not item:
            return
        vals = self.history_tree.item(item, "values")
        created, pdf, voice = vals[0], vals[1], vals[2]
        # Load the full summary for that run
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT summary_text, audio_path FROM runs WHERE created_at=?", (created,))
        row = cur.fetchone()
        conn.close()
        if row:
            self._set_summary_text(row[0])
            self.audio_out_path = row[1]
            self.save_audio_btn.configure(state="normal")
            self.pdf_path_var.set(pdf)
            self.selected_voice_name.set(voice)
            self.set_status(f"Loaded run from {created}")

    # --------- Utilities ----------
    def set_status(self, text):
        self.status_var.set(text)

    def _set_summary_text(self, text):
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        self.summary_box.insert("1.0", text)
        self.summary_box.configure(state="disabled")

    def _run_bg(self, task, on_start=None, on_success=None, on_error=None):
        def runner():
            try:
                result = task()
                self.after(0, lambda: (self.progress.stop(), on_success and on_success(result)))
            except Exception as e:
                self.after(0, lambda: (self.progress.stop(), on_error and on_error(e)))
        if on_start:
            on_start()
        self.progress.start(12)
        threading.Thread(target=runner, daemon=True).start()

# =========================
# Main
# =========================
if __name__ == "__main__":
    init_db()
    app = App()
    app.mainloop()
