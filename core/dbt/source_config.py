from dataclasses import dataclass
from typing import Dict, Any, Mapping, List
from typing_extensions import Protocol

import dbt.exceptions

from dbt.utils import deep_merge
from dbt.node_types import NodeType
from dbt.adapters.factory import get_adapter_class_by_name


class HasConfigFields(Protocol):
    seeds: Dict[str, Any]
    snapshots: Dict[str, Any]
    models: Dict[str, Any]


class IsFQNResource(Protocol):
    fqn: List[str]
    resource_type: NodeType
    package_name: str


def _listify(value) -> List:
    if isinstance(value, tuple):
        value = list(value)
    elif not isinstance(value, list):
        value = [value]

    return value


class ConfigUpdater:
    AppendListFields = {'pre-hook', 'post-hook', 'tags'}
    ExtendDictFields = {'vars', 'column_types', 'quoting', 'persist_docs'}
    DefaultClobberFields = {
        'alias',
        'schema',
        'enabled',
        'materialized',
        'unique_key',
        'database',
        'severity',
        'sql_header',
        'incremental_strategy',

        # snapshots
        'target_database',
        'target_schema',
        'strategy',
        'updated_at',
        # this is often a list, but it should replace and not append (sometimes
        # it's 'all')
        'check_cols',
        # seeds
        'quote_columns',
    }

    @property
    def ClobberFields(self):
        return self.DefaultClobberFields | self.AdapterSpecificConfigs

    @property
    def ConfigKeys(self):
        return (
            self.AppendListFields | self.ExtendDictFields | self.ClobberFields
        )

    def __init__(self, adapter_type: str):
        adapter_class = get_adapter_class_by_name(adapter_type)
        self.AdapterSpecificConfigs = adapter_class.AdapterSpecificConfigs

    def update_config_keys_into(
        self, mutable_config: Dict[str, Any], new_configs: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Update mutable_config with the contents of new_configs, but only
        include "expected" config values.

        Returns dict where the keys are what was updated and the update values
        are what the updates were.
        """

        relevant_configs: Dict[str, Any] = {
            key: new_configs[key] for key
            in new_configs if key in self.ConfigKeys
        }

        for key in self.AppendListFields:
            append_fields = _listify(relevant_configs.get(key, []))
            mutable_config[key].extend([
                f for f in append_fields if f not in mutable_config[key]
            ])

        for key in self.ExtendDictFields:
            dict_val = relevant_configs.get(key, {})
            try:
                mutable_config[key].update(dict_val)
            except (ValueError, TypeError, AttributeError):
                dbt.exceptions.raise_compiler_error(
                    'Invalid config field: "{}" must be a dict'.format(key)
                )

        for key in self.ClobberFields:
            if key in relevant_configs:
                mutable_config[key] = relevant_configs[key]

        return relevant_configs

    def update_into(
        self, mutable_config: Dict[str, Any], new_config: Mapping[str, Any]
    ) -> None:
        """Update mutable_config with the contents of new_config."""
        for key, value in new_config.items():
            if key in self.AppendListFields:
                current_list: List = _listify(mutable_config.get(key, []))
                current_list.extend(_listify(value))
                mutable_config[key] = current_list
            elif key in self.ExtendDictFields:
                current_dict: Dict = mutable_config.get(key, {})
                try:
                    current_dict.update(value)
                except (ValueError, TypeError, AttributeError):
                    dbt.exceptions.raise_compiler_error(
                        'Invalid config field: "{}" must be a dict'.format(key)
                    )
                mutable_config[key] = current_dict
            else:  # key in self.ClobberFields
                mutable_config[key] = value

    def get_project_config(
        self, model: IsFQNResource, project: HasConfigFields
    ) -> Dict[str, Any]:
        # most configs are overwritten by a more specific config, but pre/post
        # hooks are appended!
        config: Dict[str, Any] = {}
        for k in self.AppendListFields:
            config[k] = []
        for k in self.ExtendDictFields:
            config[k] = {}

        if model.resource_type == NodeType.Seed:
            model_configs = project.seeds
        elif model.resource_type == NodeType.Snapshot:
            model_configs = project.snapshots
        else:
            model_configs = project.models

        if model_configs is None:
            return config

        # mutates config
        self.update_config_keys_into(config, model_configs)

        fqn = model.fqn[:]
        for level in fqn:
            level_config = model_configs.get(level, None)
            if level_config is None:
                break

            # mutates config
            relevant_configs = self.update_config_keys_into(
                config, level_config
            )

            # TODO: does this do anything? Doesn't update_config_keys_into
            # handle the clobber case?
            clobber_configs = {
                k: v for (k, v) in relevant_configs.items()
                if k not in self.AppendListFields and
                k not in self.ExtendDictFields
            }

            config.update(clobber_configs)
            model_configs = model_configs[level]

        return config

    def get_project_vars(
        self, project_vars: Dict[str, Any],
    ):
        config: Dict[str, Any] = {}
        # this is pretty trivial, since the new project vars don't care about
        # FQNs or resource types
        self.update_config_keys_into(config, project_vars)
        return config

    def merge(self, *configs: Dict[str, Any]) -> Dict[str, Any]:
        merged_config: Dict[str, Any] = {}
        for config in configs:
            # Do not attempt to deep merge clobber fields
            config = config.copy()
            clobber = {
                key: config.pop(key) for key in list(config.keys())
                if key in self.ClobberFields
            }
            intermediary_merged = deep_merge(
                merged_config, config
            )
            intermediary_merged.update(clobber)

            merged_config.update(intermediary_merged)
        return merged_config


@dataclass
class ModelParts:
    fqn: List[str]
    resource_type: NodeType


class SourceConfig:
    def __init__(self, active_project, own_project, fqn, node_type):
        self._config = None
        # active_project is a RuntimeConfig, not a Project
        self.active_project = active_project
        self.own_project = own_project

        # TODO: have this __init__ just take a `IsFQNResource` parameter?
        self.model = ModelParts(fqn=fqn, resource_type=node_type)

        self.updater = ConfigUpdater(active_project.credentials.type)

        # the config options defined within the model
        self.in_model_config: Dict[str, Any] = {}

    def get_default(self) -> Dict[str, Any]:
        defaults = {"enabled": True, "materialized": "view"}

        if self.model.resource_type == NodeType.Seed:
            defaults['materialized'] = 'seed'
        elif self.model.resource_type == NodeType.Snapshot:
            defaults['materialized'] = 'snapshot'

        if self.model.resource_type == NodeType.Test:
            defaults['severity'] = 'ERROR'

        return defaults

    # this is re-evaluated every time `config` is called.
    # we can cache it, but that complicates things.
    # TODO : see how this fares performance-wise
    @property
    def config(self):
        """
        Config resolution order:

         if this is a dependency model:
           - own project config
           - in-model config
           - active project config
         if this is a top-level model:
           - active project config
           - in-model config
        """

        defaults = self.get_default()
        active_config = self.load_config_from_active_project()

        if self.active_project.project_name == self.own_project.project_name:
            cfg = self.updater.merge(
                defaults, active_config, self.in_model_config
            )
        else:
            own_config = self.load_config_from_own_project()

            cfg = self.updater.merge(
                defaults, own_config, self.in_model_config, active_config
            )

        return cfg

    def _translate_adapter_aliases(self, config):
        return self.active_project.credentials.translate_aliases(config)

    def update_in_model_config(self, config) -> None:
        config = self._translate_adapter_aliases(config)
        self.updater.update_into(self.in_model_config, config)

    def load_config_from_own_project(self) -> Dict[str, Any]:
        return self.updater.get_project_config(self.model, self.own_project)

    def load_config_from_active_project(self) -> Dict[str, Any]:
        return self.updater.get_project_config(self.model, self.active_project)
