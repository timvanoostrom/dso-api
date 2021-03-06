import os
import logging
from django.apps import apps, config as apps_config
from django.conf import settings

logger = logging.getLogger(__name__)


class VirtualAppConfig(apps_config.AppConfig):
    """
    Virtual App Config, allowing to add models for datasets on the fly.
    """

    def __init__(self, apps, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make django think that App is initiated already.
        self.models = dict()
        # Path is required for Django to think this APP is real.
        self.path = os.path.dirname(__file__)

        self.apps = apps
        apps.app_configs[self.label] = self

        # Disable migrations for this model.
        if not hasattr(settings, "MIGRATION_MODULES"):
            settings.MIGRATION_MODULES = dict()
        try:
            settings.MIGRATION_MODULES[self.label] = None
        except TypeError as e:
            logger.warning(f"Failed to disable migrations for {self.label}: {e}")

    def _path_from_module(self, module):
        """
        Disable OS loading for this App Config.
        """
        return None

    def register_model(self, model):
        """
        Register model in django registry and update models.
        """
        self.apps.register_model(self.label, model)
        self.models = self.apps.all_models[self.label]


def register_model(dataset, model):
    """
    Register model in django.apps
    """

    dataset_id = dataset.schema.id
    if dataset_id in apps.app_configs:
        app_config = apps.app_configs[dataset_id]
    else:
        app_config = VirtualAppConfig(apps, dataset_id, app_module=__file__)

    app_config.register_model(model)
