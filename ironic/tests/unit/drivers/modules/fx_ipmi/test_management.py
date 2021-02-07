# Copyright 2021, Linaro Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import mock

from ironic.common import boot_devices
from ironic.common import exception
from ironic.conductor import task_manager
from ironic.drivers.modules.fx_ipmi import management as fx_management
from ironic.drivers.modules import ipmitool
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils


class FxIPMITestCase(db_base.DbTestCase):

    def setUp(self):
        super(FxIPMITestCase, self).setUp()
        self.driver_info = db_utils.get_test_ipmi_info()
        self.config(enabled_hardware_types=['fx-ipmi'],
                    enabled_management_interfaces=['fx-ipmitool'],
                    enabled_power_interfaces=['ipmitool'])
        self.node = obj_utils.create_test_node(
            self.context, driver='fx-ipmi', driver_info=self.driver_info)


class FxIPMIManagementTestCase(FxIPMITestCase):

    def test_get_supported_boot_devices(self):
        with task_manager.acquire(self.context, self.node.uuid) as task:
            expected = [boot_devices.PXE, boot_devices.DISK,
                        boot_devices.BIOS]
            self.assertEqual(sorted(expected), sorted(task.driver.management.
                             get_supported_boot_devices(task)))

    @mock.patch.object(fx_management.LOG, 'warning', spec_set=True,
                       autospec=True)
    @mock.patch.object(ipmitool, "send_raw", spec_set=True, autospec=True)
    def _test_set_boot_device_ok(self, params, expected_raw_code,
                                 mock_send_raw, mock_log):
        mock_send_raw.return_value = [None, None]

        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.management.set_boot_device(task, **params)

        if params['persistent']:
            self.assertFalse(mock_log.called)
        else:
            self.assertTrue(mock_log.called)

        mock_send_raw.assert_called_once_with(mock.ANY, expected_raw_code)

    def test_set_boot_device_ok_disk(self):
        params = {'device': boot_devices.DISK, 'persistent': False}
        cmd = '0x34 0x2E 0x00'
        self._test_set_boot_device_ok(params, cmd)
        params['persistent'] = True
        self._test_set_boot_device_ok(params, cmd)

    def test_set_boot_device_ok_pxe(self):
        params = {'device': boot_devices.PXE, 'persistent': False}
        cmd = '0x34 0x2E 0x02'
        self._test_set_boot_device_ok(params, cmd)
        params['persistent'] = True
        self._test_set_boot_device_ok(params, cmd)

    def test_set_boot_device_ok_bios(self):
        params = {'device': boot_devices.BIOS, 'persistent': False}
        cmd = '0x34 0x2E 0x80'
        self._test_set_boot_device_ok(params, cmd)
        params['persistent'] = True
        self._test_set_boot_device_ok(params, cmd)

    @mock.patch.object(fx_management.LOG, 'warning', spec_set=True,
                       autospec=True)
    @mock.patch.object(ipmitool, "send_raw", spec_set=True, autospec=True)
    def test_get_boot_device_ok(self, mock_send_raw, mock_log):
        # output, expected boot device
        bootdevs = [('0x00', boot_devices.DISK),
                    ('0x02', boot_devices.PXE),
                    ('0x80', boot_devices.BIOS)]
        expected_raw_code = '0x34 0x4F'
        with task_manager.acquire(self.context, self.node.uuid) as task:
            for out, expected_device in bootdevs:
                mock_send_raw.return_value = (out, '')
                expected_response = {'boot_device': expected_device,
                                     'persistent': True}
                self.assertEqual(expected_response,
                                 task.driver.management.get_boot_device(task))
                self.assertFalse(mock_log.called)
                mock_send_raw.assert_called_with(task, expected_raw_code)

    @mock.patch.object(fx_management.LOG, 'warning', spec_set=True,
                       autospec=True)
    @mock.patch.object(ipmitool, "send_raw", spec_set=True, autospec=True)
    def test_get_boot_device_invalid_dev(self, mock_send_raw, mock_log):
        # output, expected boot device
        bootdevs = [('0x01', None),
                    ('0xFF', None)]
        expected_raw_code = '0x34 0x4F'
        with task_manager.acquire(self.context, self.node.uuid) as task:
            for out, expected_device in bootdevs:
                mock_send_raw.return_value = (out, '')
                expected_response = {'boot_device': expected_device,
                                     'persistent': True}
                self.assertEqual(expected_response,
                                 task.driver.management.get_boot_device(task))
                self.assertTrue(mock_log.called)
                mock_send_raw.assert_called_with(task, expected_raw_code)

    @mock.patch.object(ipmitool, "send_raw", spec_set=True, autospec=True)
    def test_get_boot_device_error(self, mock_send_raw):
        mock_send_raw.side_effect = exception.IPMIFailure('err')
        expected_raw_code = '0x34 0x4F'
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.assertRaises(exception.IPMIFailure,
                              task.driver.management.get_boot_device, task)
            mock_send_raw.assert_called_once_with(task, expected_raw_code)
