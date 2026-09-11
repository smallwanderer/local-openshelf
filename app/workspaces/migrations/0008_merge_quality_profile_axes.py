from django.db import migrations, models


def backfill_changed_axes(apps, schema_editor):
    WorkspaceQualityProfileRevision = apps.get_model("workspaces", "WorkspaceQualityProfileRevision")
    for revision in WorkspaceQualityProfileRevision.objects.exclude(change_axis__isnull=True).only("id", "change_axis"):
        WorkspaceQualityProfileRevision.objects.filter(pk=revision.pk).update(changed_axes=[revision.change_axis])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0007_evaluation_runs"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspacequalityprofilerevision",
            name="changed_axes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(backfill_changed_axes, noop_reverse),
        migrations.RemoveConstraint(
            model_name="workspacequalityprofilerevision",
            name="draft_quality_profile_requires_axis",
        ),
        migrations.RemoveField(
            model_name="workspacequalityprofilerevision",
            name="change_axis",
        ),
    ]
