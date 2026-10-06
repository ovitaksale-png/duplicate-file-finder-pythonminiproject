"""
Digital Duplicate File Finder
==============================
A polished desktop application for finding and safely deleting duplicate
files on your computer.

Tech stack: Python 3, customtkinter, os/pathlib, hashlib, threading

Author: Claude
Run:  python duplicate_file_finder.py

Dependency:
    pip install customtkinter
"""

import os
import hashlib
import threading
import queue
from pathlib import Path
from collections import defaultdict
from tkinter import filedialog, messagebox
import tkinter as tk

import customtkinter as ctk

# ----------------------------------------------------------------------------
# Global appearance configuration
# ----------------------------------------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CHUNK_SIZE = 65536  # 64 KB -- read files in chunks so huge files never blow up RAM

# Color palette used throughout the UI for a consistent, modern look
COLORS = {
    "bg": "#0f1117",
    "surface": "#161923",
    "surface_alt": "#1c202c",
    "accent": "#4f8cff",
    "accent_hover": "#3d75e0",
    "danger": "#ff5c5c",
    "danger_hover": "#e34a4a",
    "success": "#33d17a",
    "text_muted": "#8a90a3",
    "border": "#262b3a",
}


def format_size(num_bytes: int) -> str:
    """Format a byte count into a clean, human-readable string (KB/MB/GB)."""
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def hash_file(filepath: str, algo: str = "sha256") -> str:
    """
    Compute the hash of a file by reading it in fixed-size chunks.

    Reading in chunks (rather than file.read() all at once) is critical:
    it keeps memory usage flat regardless of file size, so hashing a
    multi-gigabyte video file uses the same tiny amount of RAM as hashing
    a 1 KB text file.
    """
    hasher = hashlib.new(algo)
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except (PermissionError, OSError):
        # Unreadable files (permissions, broken symlinks, in-use locks, etc.)
        # are skipped rather than crashing the whole scan.
        return None


class DuplicateFileFinderApp(ctk.CTk):
    """Main application window / controller."""

    def __init__(self):
        super().__init__()

        self.title("Digital Duplicate File Finder")
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.configure(fg_color=COLORS["bg"])

        # ---- State -----------------------------------------------------
        self.selected_folder = None
        self.duplicate_groups = []          # list[list[dict(path, size)]]
        self.total_files_scanned = 0
        self.checkbox_vars = {}             # filepath -> tk.BooleanVar
        self.group_frames = []
        self.scan_thread = None
        self.ui_queue = queue.Queue()       # thread -> UI communication
        self.is_scanning = False

        self._build_layout()
        self._poll_queue()

    # ------------------------------------------------------------------
    # UI CONSTRUCTION
    # ------------------------------------------------------------------
    def _build_layout(self):
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_control_bar()
        self._build_results_area()
        self._build_footer()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(24, 10))
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text="🗂️  Digital Duplicate File Finder",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#ffffff",
        )
        title.grid(row=0, column=0, sticky="w")

        subtitle = ctk.CTkLabel(
            header,
            text="Scan any folder, find identical files by content, and reclaim disk space safely.",
            font=ctk.CTkFont(size=13),
            text_color=COLORS["text_muted"],
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

    def _build_control_bar(self):
        bar = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=14)
        bar.grid(row=1, column=0, sticky="ew", padx=30, pady=10)
        bar.grid_columnconfigure(1, weight=1)

        self.select_btn = ctk.CTkButton(
            bar,
            text="📁  Select Folder",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            height=42,
            corner_radius=10,
            command=self.select_folder,
        )
        self.select_btn.grid(row=0, column=0, padx=(16, 12), pady=16)

        self.path_label = ctk.CTkLabel(
            bar,
            text="No folder selected yet",
            font=ctk.CTkFont(size=13),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        self.path_label.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        self.scan_btn = ctk.CTkButton(
            bar,
            text="🔍  Scan for Duplicates",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["success"],
            hover_color="#28ad64",
            height=42,
            corner_radius=10,
            state="disabled",
            command=self.start_scan,
        )
        self.scan_btn.grid(row=0, column=2, padx=(0, 16), pady=16)

        # Progress row (hidden until a scan starts)
        self.progress_frame = ctk.CTkFrame(bar, fg_color="transparent")
        self.progress_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 14))
        self.progress_frame.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            self.progress_frame,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        self.progress_bar = ctk.CTkProgressBar(
            self.progress_frame,
            height=10,
            corner_radius=6,
            progress_color=COLORS["accent"],
        )
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.progress_frame.grid_remove()  # hidden by default

    def _build_results_area(self):
        # Summary stats strip
        self.stats_frame = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=14)
        self.stats_frame.grid(row=2, column=0, sticky="new", padx=30, pady=(0, 10))
        for i in range(3):
            self.stats_frame.grid_columnconfigure(i, weight=1)

        self.stat_groups = self._make_stat_card(self.stats_frame, 0, "Duplicate Groups", "0")
        self.stat_files = self._make_stat_card(self.stats_frame, 1, "Files Scanned", "0")
        self.stat_space = self._make_stat_card(self.stats_frame, 2, "Space Recoverable", "0 B", value_color=COLORS["danger"])

        # Scrollable results list
        self.results_scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["bg"],
            corner_radius=0,
        )
        self.results_scroll.grid(row=3, column=0, sticky="nsew", padx=30, pady=(0, 10))
        self.results_scroll.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self.placeholder_label = ctk.CTkLabel(
            self.results_scroll,
            text="Select a folder and click \"Scan for Duplicates\" to get started.",
            font=ctk.CTkFont(size=14),
            text_color=COLORS["text_muted"],
        )
        self.placeholder_label.grid(row=0, column=0, pady=60)

    def _make_stat_card(self, parent, col, title, value, value_color="#ffffff"):
        card = ctk.CTkFrame(parent, fg_color="transparent")
        card.grid(row=0, column=col, sticky="ew", padx=20, pady=16)

        val_label = ctk.CTkLabel(
            card, text=value, font=ctk.CTkFont(size=22, weight="bold"), text_color=value_color
        )
        val_label.pack(anchor="w")

        ctk.CTkLabel(
            card, text=title, font=ctk.CTkFont(size=12), text_color=COLORS["text_muted"]
        ).pack(anchor="w")

        return val_label

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=30, pady=(0, 22))
        footer.grid_columnconfigure(0, weight=1)

        self.select_all_btn = ctk.CTkButton(
            footer,
            text="Auto-select duplicates (keep 1 per group)",
            font=ctk.CTkFont(size=12),
            fg_color="transparent",
            border_width=1,
            border_color=COLORS["border"],
            hover_color=COLORS["surface_alt"],
            text_color=COLORS["text_muted"],
            height=38,
            corner_radius=8,
            command=self.auto_select_duplicates,
        )
        self.select_all_btn.grid(row=0, column=0, sticky="w")

        self.clear_btn = ctk.CTkButton(
            footer,
            text="Clear Selection",
            font=ctk.CTkFont(size=12),
            fg_color="transparent",
            border_width=1,
            border_color=COLORS["border"],
            hover_color=COLORS["surface_alt"],
            text_color=COLORS["text_muted"],
            height=38,
            corner_radius=8,
            command=self.clear_selection,
        )
        self.clear_btn.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.delete_btn = ctk.CTkButton(
            footer,
            text="🗑  Delete Selected Duplicates",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["danger"],
            hover_color=COLORS["danger_hover"],
            height=44,
            corner_radius=10,
            command=self.confirm_and_delete,
        )
        self.delete_btn.grid(row=0, column=2, sticky="e")

    # ------------------------------------------------------------------
    # FOLDER SELECTION
    # ------------------------------------------------------------------
    def select_folder(self):
        folder = filedialog.askdirectory(title="Select a folder to scan")
        if folder:
            self.selected_folder = folder
            self.path_label.configure(text=folder, text_color="#ffffff")
            self.scan_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # SCANNING (runs in a background thread so the UI never freezes)
    # ------------------------------------------------------------------
    def start_scan(self):
        if self.is_scanning or not self.selected_folder:
            return

        self.is_scanning = True
        self._reset_results_ui()
        self.progress_frame.grid()
        self.progress_bar.set(0)
        self.select_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled", text="Scanning...")

        # Launch the heavy lifting (walking + hashing) on a daemon thread.
        # The thread never touches Tkinter widgets directly -- it only
        # pushes messages onto a thread-safe queue, which the main thread
        # polls periodically via `after()`. This is the standard safe
        # pattern for combining threading with Tkinter.
        self.scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self.scan_thread.start()

    def _scan_worker(self):
        """Background worker: walk the tree, hash files, group duplicates."""
        try:
            folder = self.selected_folder

            # --- Phase 1: enumerate all files first, so we can show a
            # meaningful progress percentage while hashing. ---
            self.ui_queue.put(("status", "Scanning for files..."))
            all_files = []
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    full_path = os.path.join(root, name)
                    all_files.append(full_path)

            total = len(all_files)
            if total == 0:
                self.ui_queue.put(("done_empty", None))
                return

            # --- Phase 2: group files by size first. Files with a unique
            # size cannot possibly be duplicates, so this cheaply skips
            # hashing the vast majority of files in a typical folder. ---
            self.ui_queue.put(("status", f"Found {total} files. Checking sizes..."))
            size_map = defaultdict(list)
            for i, path in enumerate(all_files):
                try:
                    size = os.path.getsize(path)
                    size_map[size].append(path)
                except OSError:
                    continue
                if i % 50 == 0:
                    self.ui_queue.put(("progress", (i / total) * 0.3))

            candidates = [p for group in size_map.values() if len(group) > 1 for p in group]

            # --- Phase 3: hash only the candidate files (same-size groups) ---
            hash_map = defaultdict(list)
            for i, path in enumerate(candidates):
                self.ui_queue.put(("status", f"Hashing files... ({i + 1}/{len(candidates)})"))
                digest = hash_file(path)
                if digest:
                    hash_map[digest].append(path)
                progress = 0.3 + (0.65 * ((i + 1) / len(candidates))) if candidates else 0.95
                self.ui_queue.put(("progress", progress))

            # --- Phase 4: build final duplicate groups ---
            self.ui_queue.put(("status", "Comparing hashes and grouping duplicates..."))
            groups = []
            for digest, paths in hash_map.items():
                if len(paths) > 1:
                    group = []
                    for p in paths:
                        try:
                            group.append({"path": p, "size": os.path.getsize(p)})
                        except OSError:
                            continue
                    if len(group) > 1:
                        groups.append(group)

            # Sort largest wasted-space groups first, most useful to the user
            groups.sort(key=lambda g: sum(f["size"] for f in g) - max(f["size"] for f in g), reverse=True)

            self.ui_queue.put(("progress", 1.0))
            self.ui_queue.put(("done", {"groups": groups, "total_files": total}))

        except Exception as exc:  # pragma: no cover - defensive UI safety net
            self.ui_queue.put(("error", str(exc)))

    def _poll_queue(self):
        """Runs on the main thread; drains messages from the worker thread."""
        try:
            while True:
                msg_type, payload = self.ui_queue.get_nowait()

                if msg_type == "status":
                    self.status_label.configure(text=payload)
                elif msg_type == "progress":
                    self.progress_bar.set(payload)
                elif msg_type == "done":
                    self._on_scan_complete(payload["groups"], payload["total_files"])
                elif msg_type == "done_empty":
                    self._on_scan_complete([], 0)
                elif msg_type == "error":
                    self._on_scan_error(payload)

        except queue.Empty:
            pass

        # Poll again in 100ms -- frequent enough to feel smooth, cheap enough
        # to never noticeably load the CPU.
        self.after(100, self._poll_queue)

    def _on_scan_complete(self, groups, total_files):
        self.is_scanning = False
        self.duplicate_groups = groups
        self.total_files_scanned = total_files

        self.select_btn.configure(state="normal")
        self.scan_btn.configure(state="normal", text="🔍  Scan for Duplicates")
        self.status_label.configure(text=f"Scan complete. {len(groups)} duplicate group(s) found.")
        self.progress_frame.grid_remove()

        self._render_results()
        self._update_stats()

    def _on_scan_error(self, message):
        self.is_scanning = False
        self.select_btn.configure(state="normal")
        self.scan_btn.configure(state="normal", text="🔍  Scan for Duplicates")
        self.progress_frame.grid_remove()
        messagebox.showerror("Scan Error", f"Something went wrong while scanning:\n{message}")

    # ------------------------------------------------------------------
    # RESULTS RENDERING
    # ------------------------------------------------------------------
    def _reset_results_ui(self):
        for widget in self.results_scroll.winfo_children():
            widget.destroy()
        self.checkbox_vars.clear()
        self.group_frames.clear()

    def _render_results(self):
        self._reset_results_ui()

        if not self.duplicate_groups:
            ctk.CTkLabel(
                self.results_scroll,
                text="✅ No duplicate files found. Your folder is squeaky clean!",
                font=ctk.CTkFont(size=14),
                text_color=COLORS["success"],
            ).grid(row=0, column=0, pady=60)
            return

        for idx, group in enumerate(self.duplicate_groups, start=1):
            self._render_group(idx, group)

        self.auto_select_duplicates()

    def _render_group(self, group_number, group):
        wasted = sum(f["size"] for f in group) - max(f["size"] for f in group)

        card = ctk.CTkFrame(self.results_scroll, fg_color=COLORS["surface"], corner_radius=14)
        card.grid(row=group_number - 1, column=0, sticky="ew", pady=8, padx=2)
        card.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header,
            text=f"Group #{group_number}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["accent"],
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text=f"{len(group)} copies  •  wasting {format_size(wasted)}",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_muted"],
        ).grid(row=0, column=1, sticky="e")

        # Individual file rows with checkboxes
        for row_i, file_info in enumerate(group):
            self._render_file_row(card, row_i + 1, file_info)

        # Divider padding at the bottom of the card
        ctk.CTkFrame(card, fg_color="transparent", height=6).grid(row=len(group) + 1, column=0)

    def _render_file_row(self, parent, row, file_info):
        path = file_info["path"]
        size = file_info["size"]

        row_frame = ctk.CTkFrame(parent, fg_color=COLORS["surface_alt"], corner_radius=8)
        row_frame.grid(row=row, column=0, sticky="ew", padx=18, pady=3)
        row_frame.grid_columnconfigure(1, weight=1)

        var = tk.BooleanVar(value=False)
        self.checkbox_vars[path] = var

        checkbox = ctk.CTkCheckBox(
            row_frame,
            text="",
            variable=var,
            width=20,
            checkbox_width=20,
            checkbox_height=20,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
        )
        checkbox.grid(row=0, column=0, padx=(12, 8), pady=10)

        info_frame = ctk.CTkFrame(row_frame, fg_color="transparent")
        info_frame.grid(row=0, column=1, sticky="ew", pady=8)
        info_frame.grid_columnconfigure(0, weight=1)

        filename = os.path.basename(path)
        ctk.CTkLabel(
            info_frame,
            text=filename,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#ffffff",
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            info_frame,
            text=path,
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_muted"],
            anchor="w",
        ).grid(row=1, column=0, sticky="w")

        ctk.CTkLabel(
            row_frame,
            text=format_size(size),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=COLORS["text_muted"],
        ).grid(row=0, column=2, padx=16)

    def _update_stats(self):
        total_groups = len(self.duplicate_groups)
        total_wasted = sum(
            sum(f["size"] for f in group) - max(f["size"] for f in group)
            for group in self.duplicate_groups
        )
        self.stat_groups.configure(text=str(total_groups))
        self.stat_files.configure(text=str(self.total_files_scanned))
        self.stat_space.configure(text=format_size(total_wasted))

    # ------------------------------------------------------------------
    # SELECTION HELPERS
    # ------------------------------------------------------------------
    def auto_select_duplicates(self):
        """
        Safety-first default: within each group, keep the FIRST file
        unchecked (safe / preserved) and pre-check every other copy so the
        user can review and delete with one click. Nothing is ever deleted
        without explicit confirmation regardless of checkbox state.
        """
        for group in self.duplicate_groups:
            for i, file_info in enumerate(group):
                var = self.checkbox_vars.get(file_info["path"])
                if var is not None:
                    var.set(i != 0)  # keep the first copy, select the rest

    def clear_selection(self):
        for var in self.checkbox_vars.values():
            var.set(False)

    # ------------------------------------------------------------------
    # DELETION (with a mandatory confirmation failsafe)
    # ------------------------------------------------------------------
    def confirm_and_delete(self):
        selected_paths = [p for p, var in self.checkbox_vars.items() if var.get()]

        if not selected_paths:
            messagebox.showinfo("Nothing Selected", "Please select at least one file to delete.")
            return

        # Safety check: never allow deleting every copy in any group.
        for group in self.duplicate_groups:
            group_paths = {f["path"] for f in group}
            if group_paths.issubset(set(selected_paths)):
                messagebox.showwarning(
                    "Keep At Least One Copy",
                    "At least one file in every duplicate group must remain unchecked "
                    "so you don't lose all copies of a file.",
                )
                return

        total_size = 0
        for group in self.duplicate_groups:
            for f in group:
                if f["path"] in selected_paths:
                    total_size += f["size"]

        confirmed = messagebox.askyesno(
            "Confirm Deletion",
            f"You are about to permanently delete {len(selected_paths)} file(s).\n\n"
            f"Space to be freed: {format_size(total_size)}\n\n"
            "This action cannot be undone. Continue?",
            icon="warning",
        )
        if not confirmed:
            return

        self._delete_files(selected_paths)

    def _delete_files(self, paths):
        deleted, failed = [], []

        for path in paths:
            try:
                os.remove(path)
                deleted.append(path)
            except (PermissionError, OSError) as exc:
                failed.append((path, str(exc)))

        # Remove deleted files from our in-memory groups, and drop any
        # group that no longer has duplicates.
        new_groups = []
        for group in self.duplicate_groups:
            remaining = [f for f in group if f["path"] not in deleted]
            if len(remaining) > 1:
                new_groups.append(remaining)
        self.duplicate_groups = new_groups

        self._render_results()
        self._update_stats()

        summary = f"Deleted {len(deleted)} file(s)."
        if failed:
            summary += f"\n\n{len(failed)} file(s) could not be deleted:\n"
            summary += "\n".join(f"- {os.path.basename(p)}: {err}" for p, err in failed[:8])
            if len(failed) > 8:
                summary += f"\n...and {len(failed) - 8} more."
            messagebox.showwarning("Deletion Finished With Errors", summary)
        else:
            messagebox.showinfo("Deletion Complete", summary)


if __name__ == "__main__":
    app = DuplicateFileFinderApp()
    app.mainloop()
