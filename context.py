# Copyright 2016: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import random
import uuid

import exceptions
import six
from oslo_config import cfg
from rally import consts
from rally import osclients
from rally.common import broker
from rally.common import logging
from rally.common import objects
from rally.common import utils as rutils
from rally.common.i18n import _
from rally.plugins.openstack.wrappers import keystone
from rally.plugins.openstack.wrappers import network
from rally.task import context
from rally.task import utils

LOG = logging.getLogger(__name__)

USER_CONTEXT_OPTS = [
    cfg.IntOpt("resource_management_workers",
               default=20,
               help="How many concurrent threads use for serving  context"),
    cfg.StrOpt("project_domain",
               default="default",
               help="ID of domain in which projects will be created."),
    cfg.StrOpt("user_domain",
               default="default",
               help="ID of domain in which users will be created."),
    cfg.StrOpt("keystone_default_role",
               default="member",
               help="The default role name of the keystone."),
]

CONF = cfg.CONF
CONF.register_opts(USER_CONTEXT_OPTS,
                   group=cfg.OptGroup(name="userstree_context",
                                      title="benchmark context options"))


@context.configure(name="userstree_context", order=100)
class UsersTreeContext(context.Context):
    """
        This context generates tenants arranged in a tree and users
        for these tenants.
    """

    CONFIG_SCHEMA = {
        "type": "object",
        "$schema": consts.JSON_SCHEMA,
        "properties": {
            "tree_height": {
                "type": "integer",
                "minimum": 1
            },
            "departmental_tenants": {
                "type": "integer",
                "minimum": 1
            },
            "childs_per_parent": {
                "type": "integer",
                "minimum": 1
            },
            "users_per_tenant": {
                "type": "integer",
                "minimum": 1
            },
            "resource_management_workers": {
                "type": "integer",
                "minimum": 1
            },
            "project_domain": {
                "type": "string",
            },
            "user_domain": {
                "type": "string",
            },
            "user_choice_method": {
                "enum": ["random", "round_robin"],
            },
        },
        "additionalProperties": False
    }

    DEFAULT_CONFIG = {
        "tree_height": 1,
        "departmental_tenants": 1,
        "childs_per_parent": 1,
        "users_per_tenant": 1,
        "resource_management_workers":
            cfg.CONF.users_context.resource_management_workers,
        "project_domain": cfg.CONF.userstree_context.project_domain,
        "user_domain": cfg.CONF.userstree_context.user_domain,
        "user_choice_method": "random",
    }

    def __init__(self, context):
        super(UsersTreeContext, self).__init__(context)

        self.credential = self.context["admin"]["credential"]
        self.tree_height = self.config["tree_height"]
        self.departmental_tenants = self.config["departmental_tenants"]
        self.childs_per_parent = self.config["childs_per_parent"]
        self.users_per_tenant = self.config["users_per_tenant"]

    def map_for_scenario(self, context_obj):
        """Pass one random user to scenario"""
        scenario_ctx = {}
        for key, value in six.iteritems(context_obj):
            if key not in ["tree_users", "tree_tenants"]:
                scenario_ctx[key] = value

        user = random.choice(context_obj["users"])
        tenant = context_obj["tenants"][user["tenant_id"]]

        scenario_ctx["user"], scenario_ctx["tenant"] = user, tenant
        return scenario_ctx

    def _remove_default_security_group(self):
        """Delete default security group for tenants."""
        clients = osclients.Clients(self.credential)

        if consts.Service.NEUTRON not in clients.services().values():
            return

        use_sg, msg = network.wrap(clients, self).supports_extension(
            "security-group")
        if not use_sg:
            LOG.debug("Security group context is disabled: %s" % msg)
            return

        for user, tenant_id in rutils.iterate_per_tenants(
                self.context["tree_users"]):
            with logging.ExceptionLogger(
                    LOG, _("Unable to delete default security group")):
                uclients = osclients.Clients(user["credential"])
                sg = uclients.nova().security_groups.find(name="default")
                clients.neutron().delete_security_group(sg.id)

    def _remove_associated_networks(self):
        """Delete associated Nova networks from tenants."""
        # NOTE(rmk): Ugly hack to deal with the fact that Nova Network
        # networks can only be disassociated in an admin context. Discussed
        # with boris-42 before taking this approach [LP-Bug #1350517].
        clients = osclients.Clients(self.credential)
        if consts.Service.NOVA not in clients.services().values():
            return

        nova_admin = clients.nova()

        if not utils.check_service_status(nova_admin, "nova-network"):
            return

        for net in nova_admin.networks.list():
            network_tenant_id = nova_admin.networks.get(net).project_id
            if network_tenant_id in self.context["tree_tenants"]:
                try:
                    nova_admin.networks.disassociate(net)
                except Exception as ex:
                    LOG.warning("Failed disassociate net: %(tenant_id)s. "
                                "Exception: %(ex)s" %
                                {"tenant_id": network_tenant_id, "ex": ex})

    def _create_tenants(self, parents=None):
        threads = self.config["resource_management_workers"]
        tenants = collections.deque()

        def publish(queue):
            if parents:
                # Level > 0
                for parent in parents:
                    for i in range(self.childs_per_parent):
                        args = (self.config["project_domain"], self.task["uuid"], i, parent)
                        queue.append(args)

            else:
                # Level == 0, create root
                args = (self.config["project_domain"], self.task["uuid"], 0, None)
                queue.append(args)

        def consume(cache, args):
            domain, task_id, i, parent = args

            if "client" not in cache:
                clients = osclients.Clients(self.credential)
                cache["client"] = clients.keystone()
            LOG.debug("Creating project with parent %(parent)s" % {"parent": parent})
            tenant = cache["client"].projects.create(self.generate_random_name(), domain, parent=parent)

            tenant_dict = {"id": tenant.id, "name": tenant.name, "parent_id": parent, "users": []}
            tenants.append(tenant_dict)

        # NOTE(msdubov): consume() will fill the tenants list in the closure.
        broker.run(publish, consume, threads)
        tenants_dict = collections.OrderedDict()
        for t in tenants:
            tenants_dict[t["id"]] = t

        return tenants_dict

    def _create_users(self):
        # NOTE(msdubov): This should be called after _create_tenants().
        threads = self.config["resource_management_workers"]
        default_role = cfg.CONF.users_context.keystone_default_role

        users = collections.deque()

        def publish(queue):
            for tenant_id in self.context["tree_tenants"]:
                for user_id in range(self.users_per_tenant):
                    username = self.generate_random_name()
                    password = str(uuid.uuid4())
                    args = (username, password, self.config["project_domain"],
                            self.config["user_domain"], tenant_id)
                    queue.append(args)

        def consume(cache, args):
            username, password, project_dom, user_dom, tenant_id = args
            if "client" not in cache:
                clients = osclients.Clients(self.credential)
                cache["client"] = keystone.wrap(clients.keystone())
            client = cache["client"]
            user = client.create_user(
                username, password,
                "%s@email.me" % username,
                tenant_id, user_dom,
                default_role=default_role)
            user_credential = objects.Credential(
                client.auth_url, user.name, password,
                self.context["tree_tenants"][tenant_id]["name"],
                consts.EndpointPermission.USER, client.region_name,
                project_domain_name=project_dom, user_domain_name=user_dom,
                endpoint_type=self.credential.endpoint_type,
                https_insecure=self.credential.insecure,
                https_cacert=self.credential.cacert)
            users.append({"id": user.id,
                          "credential": user_credential,
                          "tenant_id": tenant_id})

        # NOTE(msdubov): consume() will fill the users list in the closure.
        broker.run(publish, consume, threads)
        return list(users)

    def _delete_users(self):
        threads = self.config["resource_management_workers"]

        def publish(queue):
            for user in self.context["tree_users"]:
                queue.append(user["id"])

        def consume(cache, args):
            user_id = args
            if "client" not in cache:
                clients = osclients.Clients(self.credential)
                cache["client"] = keystone.wrap(clients.keystone())
            cache["client"].delete_user(user_id)

        broker.run(publish, consume, threads)
        self.context["tree_users"] = []

    def _delete_tenants(self, from_index):
        threads = self.config["resource_management_workers"]
        tenants_to_delete = []

        def publish(queue):
            for tenant_id in self.context["tree_tenants"].keys()[from_index:]:
                queue.append(tenant_id)

        def consume(cache, args):
            tenant_id = args
            if "client" not in cache:
                clients = osclients.Clients(self.credential)
                cache["client"] = keystone.wrap(clients.keystone())
            LOG.debug("Ready to delete tenant_id: %(tenant_id)s" % {"tenant_id": tenant_id})
            cache["client"].delete_project(tenant_id)
            tenants_to_delete.append(tenant_id)

        broker.run(publish, consume, threads)
        # Remove deleted tenants from context
        for tenant_id in tenants_to_delete:
            self.context["tree_tenants"].pop(tenant_id)

    @logging.log_task_wrapper(LOG.info, _("Enter context: `userstree_context`"))
    def setup(self):
        tenants = collections.OrderedDict()
        for dep_tenant in range(self.departmental_tenants):
            LOG.debug("Deprtmental tenant %(dep_tenant)d" % {"dep_tenant": dep_tenant})
            parents = None
            for tree_level in range(self.tree_height):
                expected_amount = self.childs_per_parent ** tree_level
                LOG.debug("Level %(level)d. Expected %(amount)d tenants"
                      % {"level": tree_level, "amount": expected_amount})
                tenants_on_level = self._create_tenants(parents)
                if len(tenants_on_level) < expected_amount:
                    raise exceptions.ContextSetupFailure(
                        ctx_name=self.get_name(),
                        msg=_("Failed to create the requested number of tenants."))
                parents = tenants_on_level
                tenants.update(parents)
        LOG.debug("Generated %(len)d tenants" % {"len": len(tenants)})
        self.context["tree_tenants"] = tenants

        self.context["tree_users"] = self._create_users()
        for user in self.context["tree_users"]:
            self.context["tree_tenants"][user["tenant_id"]]["users"].append(user)

        expected_users = self.users_per_tenant * len(self.context["tree_tenants"])
        if len(self.context["tree_users"]) < expected_users:
            raise exceptions.ContextSetupFailure(
                ctx_name=self.get_name(),
                msg=_("Failed to create the requested number of users."))

    @logging.log_task_wrapper(LOG.info, _("Exit context: `userstree_context`"))
    def cleanup(self):
        """Perform cleanup"""
        self._remove_default_security_group()
        self._delete_users()
        self._remove_associated_networks()
        # Delete tenants by iterating from the tail (i.e. childs first)
        for dep_tenant in range(self.departmental_tenants):
            for tree_level in reversed(range(self.tree_height)):
                expected_amount = self.childs_per_parent ** tree_level
                from_index = len(self.context["tree_tenants"]) - expected_amount
                self._delete_tenants(from_index)

        self.context["tree_tenants"] = {}


