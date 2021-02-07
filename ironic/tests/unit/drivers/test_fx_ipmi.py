# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from ironic.conductor import task_manager
from ironic.drivers.modules.fx_ipmi import management
from ironic.drivers.modules import ipmitool
from ironic.drivers.modules import iscsi_deploy
from ironic.drivers.modules.network import flat as flat_net
from ironic.drivers.modules.network import neutron as neutron_net
from ironic.drivers.modules import noop
from ironic.drivers.modules import noop_mgmt
from ironic.drivers.modules import pxe
from ironic.drivers.modules.storage import cinder
from ironic.drivers.modules.storage import noop as noop_storage
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.objects import utils as obj_utils


class FxIPMIHardwareTestCase(db_base.DbTestCase):

    def setUp(self):
        super(FxIPMIHardwareTestCase, self).setUp()
        self.config(enabled_hardware_types=['fx-ipmi'],
                    enabled_power_interfaces=['ipmitool'],
                    enabled_management_interfaces=['fx-ipmitool', 'noop'],
                    enabled_network_interfaces=['flat', 'neutron', 'noop'],
                    enabled_storage_interfaces=['cinder', 'noop'])

    def _validate_interfaces(self, task, **kwargs):
        self.assertIsInstance(
            task.driver.management,
            kwargs.get('management', management.FxIPMIManagement))
        self.assertIsInstance(
            task.driver.power,
            kwargs.get('power', ipmitool.IPMIPower))
        self.assertIsInstance(
            task.driver.boot,
            kwargs.get('boot', pxe.PXEBoot))
        self.assertIsInstance(
            task.driver.deploy,
            kwargs.get('deploy', iscsi_deploy.ISCSIDeploy))
        self.assertIsInstance(
            task.driver.console,
            kwargs.get('console', noop.NoConsole))
        self.assertIsInstance(
            task.driver.network,
            kwargs.get('network', flat_net.FlatNetwork))
        self.assertIsInstance(
            task.driver.raid,
            kwargs.get('raid', noop.NoRAID))
        self.assertIsInstance(
            task.driver.vendor,
            kwargs.get('vendor', noop.NoVendor))
        self.assertIsInstance(
            task.driver.storage,
            kwargs.get('storage', noop_storage.NoopStorage))
        self.assertIsInstance(
            task.driver.rescue,
            kwargs.get('rescue', noop.NoRescue))

    def test_default_interfaces(self):
        node = obj_utils.create_test_node(self.context, driver='fx-ipmi')
        with task_manager.acquire(self.context, node.id) as task:
            self._validate_interfaces(task)

    def test_override_with_noop_mgmt(self):
        node = obj_utils.create_test_node(
            self.context, driver='fx-ipmi',
            management_interface='noop')
        with task_manager.acquire(self.context, node.id) as task:
            self._validate_interfaces(task,
                                      management=noop_mgmt.NoopManagement)

    def test_override_with_cinder_storage(self):
        node = obj_utils.create_test_node(
            self.context, driver='fx-ipmi',
            storage_interface='cinder')
        with task_manager.acquire(self.context, node.id) as task:
            self._validate_interfaces(task, storage=cinder.CinderStorage)

    def test_override_with_neutron_network(self):
        node = obj_utils.create_test_node(
            self.context, driver='fx-ipmi',
            network_interface='neutron')
        with task_manager.acquire(self.context, node.id) as task:
            self._validate_interfaces(task, network=neutron_net.NeutronNetwork)
