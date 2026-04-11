"""
Eksportuje kolejność sekcji i forów jako JSON lub CSV.

Użycie:
    python manage.py export_forum_order
    python manage.py export_forum_order --format csv
    python manage.py export_forum_order --output kolejnosc.json
"""

import csv
import json
import sys

from django.core.management.base import BaseCommand

from board.models import Forum, Section


class Command(BaseCommand):
    help = "Export section and forum order to JSON or CSV"

    def add_arguments(self, parser):
        parser.add_argument(
            "--format", choices=["json", "csv"], default="json",
            help="Output format (default: json)",
        )
        parser.add_argument(
            "--output", default="",
            help="Output file path (default: stdout)",
        )

    def handle(self, *args, **options):
        sections = list(
            Section.objects.order_by("order").values("id", "title", "order")
        )
        forums = list(
            Forum.objects.order_by("order").values(
                "id", "title", "order", "parent_id", "section_id"
            )
        )

        fmt = options["format"]
        out_path = options["output"]

        if fmt == "json":
            data = {"sections": sections, "forums": forums}
            content = json.dumps(data, ensure_ascii=False, indent=2)
            if out_path:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.stdout.write(f"Zapisano: {out_path}")
            else:
                self.stdout.write(content)

        else:  # csv
            def write_csv(fileobj):
                w = csv.writer(fileobj)
                w.writerow(["type", "id", "title", "order", "parent_id", "section_id"])
                for s in sections:
                    w.writerow(["section", s["id"], s["title"], s["order"], "", ""])
                for f in forums:
                    w.writerow(["forum", f["id"], f["title"], f["order"],
                                f["parent_id"] or "", f["section_id"] or ""])

            if out_path:
                with open(out_path, "w", encoding="utf-8", newline="") as f:
                    write_csv(f)
                self.stdout.write(f"Zapisano: {out_path}")
            else:
                write_csv(sys.stdout)
