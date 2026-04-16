from django.db import migrations


class Migration(migrations.Migration):
    """Drop api_post_reports table — reports unified into board.PostReport (forum_post_report)."""

    dependencies = [
        ("api", "0002_rename_api_postreport_post_idx_api_post_re_post_id_8b4954_idx_and_more"),
        ("board", "0072_postreport_resolution_reason_extend"),
    ]

    operations = [
        migrations.DeleteModel(name="PostReport"),
    ]
