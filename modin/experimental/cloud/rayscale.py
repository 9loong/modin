# Licensed to Modin Development Team under one or more contributor license agreements.
# See the NOTICE file distributed with this work for additional information regarding
# copyright ownership.  The Modin Development Team licenses this file to you under the
# Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

import threading
import os
import traceback
import sys
from shlex import quote
from hashlib import sha1
from typing import Callable

import yaml
from ray.autoscaler.commands import (
    create_or_update_cluster,
    teardown_cluster,
    get_head_node_ip,
)

from .base import CannotSpawnCluster, CannotDestroyCluster, ConnectionDetails
from .cluster import Cluster, Provider


class _ThreadTask:
    def __init__(self, target: Callable):
        self.target = target
        self.thread: threading.Thread = None
        self.exc: Exception = None
        self.silent = False


class RayCluster(Cluster):
    __base_config = os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "ray-autoscaler.yml"
    )
    __instance_key = {Provider.AWS: "InstanceType"}
    __credentials_env = {Provider.AWS: "AWS_CONFIG_FILE"}

    def __init__(self, *a, **kw):
        self.spawner = _ThreadTask(self.__spawn)
        self.destroyer = _ThreadTask(self.__destroy)

        self.ready = False
        super().__init__(self, *a, **kw)

        if self.provider.credentials_file is not None:
            try:
                config_key = self.__credentials_env[self.provider.name]
            except KeyError:
                raise ValueError(f"Unsupported provider: {self.provider.name}")
            os.environ[config_key] = self.provider.credentials_file

        self.config = self.__make_config()
        self.config_file = self.__save_config(self.config)

    def spawn(self, wait=True):
        """
        Actually spawns the cluster. When already spawned, should be a no-op.

        When wait==False it spawns cluster asynchronously.
        """
        self.__run_thread(wait, self.spawner)

    def destroy(self, wait=True):
        """
        Destroys the cluster. When already destroyed, should be a no-op.
        """
        self.__run_thread(wait, self.destroyer)

    def __run_thread(self, wait, task: _ThreadTask):
        if not task.thread:
            task.thread = threading.Thread(target=task.target)

        if wait:
            task.silent = True
            task.thread.join()
            exc, task.exc = task.exc, None
            if exc:
                raise exc

    def __make_config(self):
        with open(self.__base_config) as inp:
            config = yaml.safe_load(inp.read())

        # cluster and provider details
        config["cluster_name"] = self.cluster_name
        config["min_workers"] = self.worker_count
        config["max_workers"] = self.worker_count
        config["initial_workers"] = self.worker_count
        config["provider"]["type"] = self.provider.name
        if self.provider.region:
            config["provider"]["region"] = self.provider.region
        if self.provider.zone:
            config["provider"]["zone"] = self.provider.zone

        # connection details
        socks_proxy = os.environ.get("MODIN_SOCKS_PROXY", None)
        config["auth"]["ssh_user"] = "modin"
        if socks_proxy:
            config["auth"]["ssh_proxy_command"] = f"nc -x {quote(socks_proxy)} %h %p"

        # instance types
        try:
            instance_key = self.__instance_key[self.provider.name]
        except KeyError:
            raise ValueError(f"Unsupported provider: {self.provider.name}")
        config["head_node"][instance_key] = self.head_node_type
        config["worker_nodes"][instance_key] = self.worker_node_type

        return config

    @staticmethod
    def __save_config(config):
        cfgdir = os.path.abspath(os.path.expanduser("~/.modin/cloud"))
        os.makedirs(cfgdir, mode=0o700, exist_ok=True)
        namehash = sha1(repr(config).encode("utf8")).hexdigest()
        for stop in range(4, len(namehash)):
            entry = os.path.join(cfgdir, f"config-{namehash[:stop]}.yml")
            if not os.path.exists(entry):
                break
        else:
            entry = os.path.join(cfgdir, f"config-{namehash}.yml")

        with open(entry, "w") as out:
            out.write(yaml.dump(config))
        return entry

    def __spawn(self):
        try:
            create_or_update_cluster(
                self.config_file,
                override_min_workers=None,
                override_max_workers=None,
                no_restart=False,
                restart_only=False,
                yes=True,
                override_cluster_name=None,
            )
            # need to re-load the config, as create_or_update_cluster() modifies it
            with open(self.config_file) as inp:
                self.config = yaml.safe_load(inp.read())
            self.ready = True
        except BaseException as ex:
            self.spawn_exc = CannotSpawnCluster("Cannot spawn cluster", cause=ex)
            if not self.spawner.silent:
                sys.stderr.write(f"Cannot spawn cluster:\n{traceback.format_exc()}\n")

    def __destroy(self):
        try:
            teardown_cluster(
                self.config_file,
                yes=True,
                workers_only=False,
                override_cluster_name=None,
                keep_min_workers=0,
            )
            self.ready = False
            self.config = None
        except BaseException as ex:
            self.destroy_exc = CannotDestroyCluster("Cannot destroy cluster", cause=ex)
            if not self.destroyer.silent:
                sys.stderr.write(f"Cannot destroy cluster:\n{traceback.format_exc()}\n")

    def _get_connection_details(self) -> ConnectionDetails:
        """
        Gets the coordinates on how to connect to cluster frontend node.
        """
        assert self.ready, "Cluster is not ready, cannot get connection details"
        return ConnectionDetails(
            user_name=self.config["auth"]["ssh_user"],
            key_file=self.config["auth"]["ssh_private_key"],
            address=get_head_node_ip(self.config_file, override_cluster_name=None),
        )

    def _get_main_python(self) -> str:
        """
        Gets the path to 'main' interpreter (the one that houses created environment for running everything)
        """
        return "~/miniconda/envs/modin/bin/python"
