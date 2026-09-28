"""
SubSync - batch-synchronise .srt subtitles to the audio of their video files.

A small Tkinter front end for ffsubsync (https://github.com/smacke/ffsubsync).

Per subtitle file:
  - the original is kept as <name>.srt.bak (media servers like Jellyfin/Plex ignore it);
    every run starts from that backup, so re-running is always safe
  - run 1: constant offset only (--no-fix-framerate)
  - run 2: offset + framerate search (--gss)
  - --gss is used only if it scores >= 10% better and actually changes the framerate
  - if the detected offset is < 0.3 s (and no framerate change), the file is left untouched

Requirements: Python 3.10+ with Tkinter, ffmpeg on PATH, ffsubsync.
See README.md for installation.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

__version__ = "1.0.0"

APP_NAME = "SubSync"
IS_WIN = os.name == "nt"
DATA_DIR = (Path(os.environ["APPDATA"]) if IS_WIN and os.environ.get("APPDATA")
            else Path.home() / ".config") / APP_NAME
CFG_PATH = DATA_DIR / "config.json"
VENV_DIR = (Path(os.environ["LOCALAPPDATA"]) if IS_WIN and os.environ.get("LOCALAPPDATA")
            else Path.home() / ".local" / "share") / APP_NAME / "venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
GSS_MIN_GAIN = 1.10   # --gss must score at least 10% higher than a constant offset
MIN_OFFSET = 0.3      # seconds; smaller offsets are treated as "already in sync"


# ---------------------------------------------------------------- environment
def console_python():
    """python(.exe) next to the running interpreter (also when started via pythonw)."""
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists():
        py = py.with_name("python.exe")
    return str(py)


def augment_path():
    """Add Python script dirs and WinGet dirs to PATH (also for child processes).
    Apps launched from Explorer only see PATH changes after a new login."""
    import sysconfig
    dirs = [Path(sys.executable).parent, Path(sys.executable).parent / "Scripts"]
    for scheme in (None, f"{os.name}_user"):
        try:
            dirs.append(Path(sysconfig.get_path("scripts", scheme) if scheme
                             else sysconfig.get_path("scripts")))
        except (KeyError, TypeError):
            pass
    la = os.environ.get("LOCALAPPDATA")
    if la:
        dirs.append(Path(la) / "Microsoft" / "WinGet" / "Links")
        dirs += sorted(Path(la, "Microsoft", "WinGet", "Packages").glob("Gyan.FFmpeg*/*/bin"))
    cur = os.environ.get("PATH", "")
    extra = [str(d) for d in dirs if d.is_dir() and str(d) not in cur]
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [cur])


def has_ffsubsync(python):
    try:
        r = subprocess.run([python, "-c", "import ffsubsync"], capture_output=True,
                           creationflags=NO_WINDOW, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_ffs_cmd():
    # 1. dedicated venv (recommended; needed on Python versions without webrtcvad wheels)
    if VENV_PY.exists() and has_ffsubsync(str(VENV_PY)):
        return [str(VENV_PY), "-m", "ffsubsync.ffsubsync"]
    # 2. the interpreter running this app
    if has_ffsubsync(console_python()):
        return [console_python(), "-m", "ffsubsync.ffsubsync"]
    # 3. anything on PATH
    for name in ("ffs", "ffsubsync"):
        p = shutil.which(name)
        if p:
            return [p]
    return None


# ---------------------------------------------------------------- sync logic
def parse_val(text, key):
    found = re.findall(rf"{re.escape(key)}:\s*(-?\d+(?:\.\d+)?)", text)
    return float(found[-1]) if found else None


def find_video(srt: Path):
    """Movie.srt / Movie.en.srt -> Movie.<video ext>"""
    name = srt.stem
    base = name.rsplit(".", 1)[0] if "." in name else name
    for n in (name, base):
        for ext in VIDEO_EXT:
            p = srt.with_name(n + ext)
            if p.exists():
                return p
    return None


def backup_of(srt: Path):
    return srt.with_name(srt.name + ".bak")


class Syncer:
    def __init__(self, ffs_cmd):
        self.ffs_cmd = ffs_cmd
        self.stop_event = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()

    def _run(self, video, inp, outp, extra):
        cmd = self.ffs_cmd + [str(video), "--reference-stream", "a:0", *extra,
                              "-i", str(inp), "-o", str(outp)]
        env = dict(os.environ, COLUMNS="400", PYTHONIOENCODING="utf-8", NO_COLOR="1")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                env=env, creationflags=NO_WINDOW)
        with self._lock:
            self._procs.add(proc)
        try:
            out, _ = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        return proc.returncode, out or ""

    def kill_all(self):
        self.stop_event.set()
        with self._lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass

    def process(self, srt: Path):
        """Returns dict: status, method, offset, factor, info."""
        if self.stop_event.is_set():
            return dict(status="stopped")
        video = find_video(srt)
        if not video:
            return dict(status="SKIP", info="no matching video found")

        bak = backup_of(srt)
        if not bak.exists():
            shutil.copy2(srt, bak)

        with tempfile.TemporaryDirectory(prefix="subsync_") as td:
            tin, ta, tb = Path(td) / "in.srt", Path(td) / "a.srt", Path(td) / "b.srt"
            shutil.copyfile(bak, tin)   # ffsubsync needs a .srt extension

            _, out_a = self._run(video, tin, ta, ["--no-fix-framerate"])
            if self.stop_event.is_set():
                return dict(status="stopped")
            _, out_b = self._run(video, tin, tb, ["--gss"])
            if self.stop_event.is_set():
                return dict(status="stopped")

            sa, oa = parse_val(out_a, "score"), parse_val(out_a, "offset seconds")
            sb, ob = parse_val(out_b, "score"), parse_val(out_b, "offset seconds")
            fb = parse_val(out_b, "framerate scale factor")
            a_ok = sa is not None and ta.exists() and ta.stat().st_size > 0
            b_ok = sb is not None and tb.exists() and tb.stat().st_size > 0

            if not a_ok and not b_ok:
                shutil.copyfile(bak, srt)
                last = (out_b or out_a).strip().splitlines()[-1:] or ["unknown error"]
                return dict(status="FAIL", info=last[0][:200])

            use_gss = b_ok and fb is not None and abs(fb - 1.0) > 1e-9 and \
                (not a_ok or sb >= sa * GSS_MIN_GAIN)

            if use_gss:
                shutil.copyfile(tb, srt)
                return dict(status="OK", method="gss", offset=ob, factor=fb,
                            info=f"score {sb:.0f} vs {sa if sa is not None else 0:.0f}")
            if a_ok and abs(oa or 0) < MIN_OFFSET:
                shutil.copyfile(bak, srt)
                return dict(status="IN SYNC", method="none", offset=oa, factor=1.0,
                            info="offset too small, left unchanged")
            if a_ok:
                shutil.copyfile(ta, srt)
                return dict(status="OK", method="offset", offset=oa, factor=1.0,
                            info=f"score {sa:.0f} vs gss {sb if sb is not None else 0:.0f}")
            shutil.copyfile(tb, srt)
            return dict(status="OK", method="gss", offset=ob, factor=fb or 1.0,
                        info="only gss succeeded")


# ---------------------------------------------------------------- config
def load_cfg():
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    try:
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    COLS = ("status", "method", "offset", "factor", "info")
    TAGS = ("OK", "IN SYNC", "FAIL", "SKIP", "busy")

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__} — subtitle synchroniser")
        self.geometry("1150x680")
        self.minsize(800, 450)

        self.cfg = load_cfg()
        self.folder = tk.StringVar(value=self.cfg.get("folder", ""))
        self.workers = tk.IntVar(value=int(self.cfg.get("workers", 4)))
        self.only_new = tk.BooleanVar(value=bool(self.cfg.get("only_new", True)))
        self.q = queue.Queue()
        self.syncer = None
        self.running = False
        self.files = []

        augment_path()
        self.ffs_cmd = find_ffs_cmd()
        self._build()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        ffmpeg = shutil.which("ffmpeg")
        self.log_line(f"Python:    {console_python()}  ({sys.version.split()[0]})")
        self.log_line(f"ffsubsync: {' '.join(self.ffs_cmd) if self.ffs_cmd else 'NOT FOUND'}")
        self.log_line(f"ffmpeg:    {ffmpeg or 'NOT FOUND'}")

        if not ffmpeg:
            messagebox.showwarning(APP_NAME, "ffmpeg was not found.\n\n"
                                   "Install it (Windows: winget install Gyan.FFmpeg) "
                                   "and restart the app.")
        if not self.ffs_cmd:
            self.after(300, self.offer_install)
        elif self.folder.get() and Path(self.folder.get()).is_dir():
            self.after(200, self.scan)

    # ---- layout
    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse…", command=self.choose).pack(side="left")
        ttk.Button(top, text="Scan", command=self.scan).pack(side="left", padx=(6, 0))

        bar = ttk.Frame(self, padding=(8, 0, 8, 6))
        bar.pack(fill="x")
        self.btn_new = ttk.Button(bar, text="Sync new", command=lambda: self.start("new"))
        self.btn_sel = ttk.Button(bar, text="Sync selected", command=lambda: self.start("sel"))
        self.btn_all = ttk.Button(bar, text="Sync all", command=lambda: self.start("all"))
        self.btn_restore = ttk.Button(bar, text="Restore original", command=self.restore)
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        for b in (self.btn_new, self.btn_sel, self.btn_all, self.btn_restore, self.btn_stop):
            b.pack(side="left", padx=(0, 6))
        ttk.Checkbutton(bar, text="Show new only", variable=self.only_new,
                        command=self.refresh_view).pack(side="left", padx=(12, 0))
        ttk.Label(bar, text="Parallel:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(bar, from_=1, to=16, width=4, textvariable=self.workers).pack(side="left")
        self.progress = ttk.Progressbar(bar, length=200, mode="determinate")
        self.progress.pack(side="right")
        self.lbl_prog = ttk.Label(bar, text="")
        self.lbl_prog.pack(side="right", padx=6)

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        tf = ttk.Frame(pane)
        self.tree = ttk.Treeview(tf, columns=self.COLS, selectmode="extended")
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=480)
        widths = {"status": 80, "method": 70, "offset": 80, "factor": 70, "info": 300}
        for c in self.COLS:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c == "info"))
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        for tag, col in zip(self.TAGS, ("#1a7f37", "#0969da", "#cf222e", "#9a6700", "#8250df")):
            self.tree.tag_configure(tag, foreground=col)
        pane.add(tf, weight=4)

        lf = ttk.Frame(pane)
        self.log = tk.Text(lf, height=8, wrap="none", font=("Consolas", 9))
        ls = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")
        pane.add(lf, weight=1)

    # ---- helpers
    def log_line(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def root_path(self):
        return Path(self.folder.get())

    def rel(self, p: Path):
        try:
            return str(p.relative_to(self.root_path()))
        except ValueError:
            return str(p)

    @staticmethod
    def is_new(p: Path):
        return not backup_of(p).exists()

    def set_row(self, p: Path, status="", method="", offset=None, factor=None, info=""):
        iid = str(p)
        vals = (status, method,
                "" if offset is None else f"{offset:+.2f} s",
                "" if factor is None else f"{factor:.3f}", info)
        tag = (status,) if status in self.TAGS else ()
        if self.tree.exists(iid):
            self.tree.item(iid, values=vals, tags=tag)
        elif not self.only_new.get() or self.is_new(p) or status:
            self.tree.insert("", "end", iid=iid, text=self.rel(p), values=vals, tags=tag)

    def _busy(self, busy):
        self.running = busy
        st = "disabled" if busy else "normal"
        for b in (self.btn_new, self.btn_sel, self.btn_all, self.btn_restore):
            b.configure(state=st)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # ---- install helper
    def offer_install(self):
        py = str(VENV_PY) if VENV_PY.exists() else console_python()
        if not VENV_PY.exists() and sys.version_info >= (3, 14):
            self.log_line("ffsubsync cannot be installed on Python 3.14+ yet (webrtcvad has no "
                          "wheels). Create a Python 3.12 environment for it:")
            if IS_WIN:
                self.log_line("  py install 3.12")
                self.log_line(f'  py -V:3.12 -m venv "{VENV_DIR}"')
            else:
                self.log_line(f'  python3.12 -m venv "{VENV_DIR}"')
            self.log_line(f'  "{VENV_PY}" -m pip install ffsubsync')
            self.log_line("then restart SubSync.")
            messagebox.showwarning(APP_NAME, "ffsubsync cannot be installed on this Python "
                                   "version.\n\nThe commands to create a Python 3.12 environment "
                                   "are shown in the log panel.")
            return
        if not messagebox.askyesno(
                APP_NAME, f"ffsubsync is not installed for this Python:\n\n{py}\n\n"
                "Install it now? (takes about a minute)"):
            self.log_line("ffsubsync missing — syncing is not possible.")
            return
        self._busy(True)
        self.btn_stop.configure(state="disabled")
        pip_args = ["install", "ffsubsync"] if VENV_PY.exists() else ["install", "--user", "ffsubsync"]
        self.log_line(f"Installing: {py} -m pip {' '.join(pip_args)} …")

        def work():
            try:
                proc = subprocess.Popen([py, "-m", "pip", *pip_args],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=NO_WINDOW)
                for line in proc.stdout:
                    if line.strip():
                        self.q.put(("log", line.rstrip()))
                proc.wait()
                ok = proc.returncode == 0
            except OSError as e:
                self.q.put(("log", f"Error: {e}"))
                ok = False
            self.q.put(("installed", ok))
        threading.Thread(target=work, daemon=True).start()

    # ---- actions
    def choose(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or str(Path.home()))
        if d:
            self.folder.set(d)
            self.scan()

    def scan(self):
        root = self.root_path()
        if not root.is_dir():
            messagebox.showerror(APP_NAME, f"Folder does not exist:\n{root}")
            return
        self.cfg["folder"] = str(root)
        save_cfg(self.cfg)
        self.log_line(f"Scanning: {root} …")
        self._busy(True)
        self.btn_stop.configure(state="disabled")

        def work():
            files = sorted(p for p in root.rglob("*.srt") if p.is_file())
            self.q.put(("scanned", files))
        threading.Thread(target=work, daemon=True).start()

    def refresh_view(self):
        self.cfg["only_new"] = self.only_new.get()
        save_cfg(self.cfg)
        self.tree.delete(*self.tree.get_children())
        for p in self.files:
            if not self.only_new.get() or self.is_new(p):
                self.tree.insert("", "end", iid=str(p), text=self.rel(p),
                                 values=("new" if self.is_new(p) else "processed", "", "", "", ""))
        n_new = sum(1 for p in self.files if self.is_new(p))
        self.lbl_prog.configure(text=f"{len(self.files)} srt, {n_new} new")

    def start(self, mode):
        if self.running or not self.ffs_cmd:
            return
        if mode == "new":
            targets = [p for p in self.files if self.is_new(p)]
        elif mode == "sel":
            targets = [Path(i) for i in self.tree.selection()]
        else:
            targets = list(self.files)
            if targets and not messagebox.askyesno(
                    APP_NAME, f"Re-sync all {len(targets)} files (starting from the backups)?"):
                return
        if not targets:
            messagebox.showinfo(APP_NAME, "Nothing to do.")
            return

        try:
            n = max(1, min(16, int(self.workers.get())))
        except (tk.TclError, ValueError):
            n = 4
        self.cfg["workers"] = n
        save_cfg(self.cfg)
        self._busy(True)
        self.progress.configure(maximum=len(targets), value=0)
        self.done_count, self.total = 0, len(targets)
        self.lbl_prog.configure(text=f"0/{self.total}")
        self.log_line(f"Start: {len(targets)} file(s), {n} in parallel")
        self.syncer = Syncer(self.ffs_cmd)

        def one(p):
            self.q.put(("row", p, dict(status="busy")))
            try:
                res = self.syncer.process(p)
            except Exception as e:  # noqa: BLE001
                res = dict(status="FAIL", info=str(e)[:200])
            self.q.put(("row", p, res))
            self.q.put(("tick", p, res))

        def work():
            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(one, targets))
            self.q.put(("finished", None))
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        if self.syncer:
            self.log_line("Stopping…")
            self.syncer.kill_all()

    def restore(self):
        sel = [Path(i) for i in self.tree.selection()]
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more files in the list first.")
            return
        n = 0
        for p in sel:
            bak = backup_of(p)
            if bak.exists():
                shutil.copyfile(bak, p)
                self.set_row(p, status="restored", info="original restored (backup kept)")
                n += 1
        self.log_line(f"{n} file(s) restored to original.")

    # ---- queue polling
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "scanned":
                    self.files = msg[1]
                    self._busy(False)
                    self.refresh_view()
                    self.log_line(f"{len(self.files)} .srt found, "
                                  f"{sum(1 for p in self.files if self.is_new(p))} new.")
                elif kind == "row":
                    _, p, r = msg
                    self.set_row(p, r.get("status", ""), r.get("method", ""),
                                 r.get("offset"), r.get("factor"), r.get("info", ""))
                    if r.get("status") == "busy" and self.tree.exists(str(p)):
                        self.tree.see(str(p))
                elif kind == "tick":
                    _, p, r = msg
                    self.done_count += 1
                    self.progress.configure(value=self.done_count)
                    self.lbl_prog.configure(text=f"{self.done_count}/{self.total}")
                    off, fac = r.get("offset"), r.get("factor")
                    extra = f" offset {off:+.2f}s" if off is not None else ""
                    extra += f" factor {fac:.3f}" if fac not in (None, 1.0) else ""
                    self.log_line(f"{r.get('status', ''):8} {self.rel(p)}{extra}"
                                  f"{'  — ' + r['info'] if r.get('info') else ''}")
                elif kind == "finished":
                    self._busy(False)
                    self.log_line("Done.")
                elif kind == "log":
                    self.log_line(msg[1])
                elif kind == "installed":
                    self._busy(False)
                    augment_path()
                    self.ffs_cmd = find_ffs_cmd()
                    if msg[1] and self.ffs_cmd:
                        self.log_line(f"ffsubsync installed: {' '.join(self.ffs_cmd)}")
                        if self.folder.get() and Path(self.folder.get()).is_dir():
                            self.scan()
                    else:
                        self.log_line("Installation failed — see the messages above.")
                        messagebox.showerror(APP_NAME, "Installing ffsubsync failed.\n"
                                             "See the log panel for details.")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_close(self):
        if self.running and not messagebox.askyesno(APP_NAME, "A sync is running. Quit anyway?"):
            return
        if self.syncer:
            self.syncer.kill_all()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
