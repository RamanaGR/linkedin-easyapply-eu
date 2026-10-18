"""Application CSV logging and end-of-run summary."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.utils import project_path

STATUSES = (
    "APPLIED",
    "FAILED_VALIDATION",
    "FAILED_EXCEPTION",
    "SKIPPED_ALREADY_APPLIED",
    "SKIPPED_BLACKLIST",
    "SKIPPED_LANGUAGE",
    "DRY_RUN",
    "SKIPPED_SEEN",
    "STOPPED_EASY_APPLY_LIMIT",
)

CSV_FIELDS = [
    "timestamp",
    "status",
    "job_id",
    "title",
    "company",
    "location",
    "link",
    "notes",
]


@dataclass
class JobResult:
    status: str
    job_id: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    link: str = ""
    notes: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class Reporter:
    def __init__(self) -> None:
        self.results: list[JobResult] = []
        self.scanned = 0
        self.path = project_path("output", "applications.csv")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path = project_path("output", "seen_jobs.csv")
        self._ensure_csv()
        self.seen_ids = self._load_seen()

    def _ensure_csv(self) -> None:
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

    def _load_seen(self) -> set[str]:
        ids: set[str] = set()
        if not self.seen_path.exists():
            return ids
        with self.seen_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                jid = (row.get("job_id") or "").strip()
                if jid:
                    ids.add(jid)
        return ids

    def mark_seen(self, job_id: str, status: str) -> None:
        if not job_id:
            return
        self.seen_ids.add(job_id)
        exists = self.seen_path.exists()
        with self.seen_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["job_id", "status", "timestamp"])
            if not exists:
                writer.writeheader()
            writer.writerow(
                {
                    "job_id": job_id,
                    "status": status,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            )

    def record(self, result: JobResult) -> None:
        self.results.append(result)
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writerow(
                {
                    "timestamp": result.timestamp,
                    "status": result.status,
                    "job_id": result.job_id,
                    "title": result.title,
                    "company": result.company,
                    "location": result.location,
                    "link": result.link,
                    "notes": result.notes,
                }
            )
        # Only permanently skip successful / intentional terminal outcomes.
        # DRY_RUN and FAILED_* stay retryable (dry-run is practice; failures may flake).
        if result.job_id and result.status in (
            "APPLIED",
            "SKIPPED_ALREADY_APPLIED",
        ):
            self.mark_seen(result.job_id, result.status)

    def summary_counts(self) -> Counter:
        return Counter(r.status for r in self.results)

    def write_summary_md(self) -> Path:
        reports = project_path("output", "reports")
        reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        path = reports / f"run_summary_{stamp}.md"
        counts = self.summary_counts()
        applied = [r for r in self.results if r.status == "APPLIED"]
        lines = [
            f"# Run summary {stamp}",
            "",
            f"- Total scanned: {self.scanned}",
            f"- Attempted: {len(self.results)}",
            f"- Applied: {counts.get('APPLIED', 0)}",
            f"- Failed: {counts.get('FAILED_VALIDATION', 0) + counts.get('FAILED_EXCEPTION', 0)}",
            f"- Skipped: {sum(v for k, v in counts.items() if k.startswith('SKIPPED_'))}",
            f"- Dry run: {counts.get('DRY_RUN', 0)}",
            "",
            "## Counts by status",
            "",
        ]
        for status, n in sorted(counts.items()):
            lines.append(f"- {status}: {n}")
        lines.extend(["", "## Applied jobs", ""])
        if not applied:
            lines.append("_None_")
        else:
            lines.append("| Company | Title | Location | Timestamp | Link |")
            lines.append("|---|---|---|---|---|")
            for r in applied:
                lines.append(
                    f"| {r.company} | {r.title} | {r.location} | {r.timestamp} | {r.link} |"
                )
        unanswered = project_path("output", "unanswered_questions.csv")
        lines.extend(["", "## Unanswered questions", ""])
        if unanswered.exists():
            with unanswered.open(encoding="utf-8") as f:
                lines.append("```")
                lines.append(f.read().strip() or "(empty)")
                lines.append("```")
        else:
            lines.append("_None captured_")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def print_console_summary(self) -> None:
        counts = self.summary_counts()
        print("\n========== RUN SUMMARY ==========")
        print(f"Scanned: {self.scanned}  Recorded: {len(self.results)}")
        for status, n in sorted(counts.items()):
            print(f"  {status:28} {n}")
        print("=================================\n")
