from typing import Iterable

from django.core.management import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction

from dso_api.datasets.models import Dataset
from dso_api.lib.schematools.models import schema_models_factory


class Command(BaseCommand):
    help = "Create the tables based on the uploaded Amsterdam schema's."

    def handle(self, *args, **options):
        create_tables(self, Dataset.objects.all())


def create_tables(command: BaseCommand, datasets: Iterable[Dataset]):
    """Create tables for all updated datasets.
    This is a separate function to allow easy reuse.
    """
    errors = 0
    command.stdout.write(f"Creating tables")

    # First create all models. This allows Django to resolve  model relations.
    models = []
    for dataset in datasets:
        models.extend(schema_models_factory(dataset.schema))

    # Create all tables
    with connection.schema_editor() as schema_editor:
        for model in models:
            try:
                command.stdout.write(f"* Creating table {model._meta.db_table}")
                with transaction.atomic():
                    schema_editor.create_model(model)
            except (DatabaseError, ValueError) as e:
                command.stderr.write(f"  Tables not created: {e}")
                errors += 1

    if errors:
        raise CommandError("Not all tables could be created")