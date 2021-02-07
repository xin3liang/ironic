# coding=utf-8

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

"""
FUJITSU Supercomputer PRIMEHPC FX700 management interface.

"""

from ironic_lib import metrics_utils
from oslo_log import log as logging

from ironic.common import boot_devices
from ironic.common import exception
from ironic.common.i18n import _
from ironic.conductor import task_manager
from ironic.drivers.modules import ipmitool


LOG = logging.getLogger(__name__)

METRICS = metrics_utils.get_metrics_logger(__name__)

# NOTE(xinliang): FX700 boot devices OEM cmds. For more information about
# these values see the "4.1.4 OEM Command Table" of bellow link.
# https://www.fujitsu.com/downloads/SUPER/manual/c120-0091-05en.pdf
FX_OEM_BOOTDEV_NETFN = '0x34'
FX_OEM_BOOTDEV_CMD_SET = '0x2E'
FX_OEM_BOOTDEV_CMD_GET = '0x4F'
FX_OEM_BOOTDEV_HEXA_MAP = {
    boot_devices.DISK: '0x00',
    boot_devices.PXE: '0x02',
    boot_devices.BIOS: '0x80'  # boot into UEFI shell
}


class FxIPMIManagement(ipmitool.IPMIManagement):

    def get_supported_boot_devices(self, task):
        """Get a list of the supported boot devices.

        :param task: a task from TaskManager.
        :returns: A list with the supported boot devices defined
                  in :mod:`ironic.common.boot_devices`.

        """
        return list(FX_OEM_BOOTDEV_HEXA_MAP)

    @METRICS.timer('FxIPMIManagement.set_boot_device')
    @task_manager.require_exclusive_lock
    def set_boot_device(self, task, device, persistent=True):
        """Set the boot device for the task's node.

        Set the boot device to use on next reboot of the node.

        :param task: a task from TaskManager.
        :param device: the boot device, one of
                       :mod:`ironic.common.boot_devices`.
        :param persistent: Boolean value. True if the boot device will
                           persist to all future boots, False if not.
                           Default: True. FX only supports persistent
                           setting.
        :raises: InvalidParameterValue if an invalid boot device is specified
        :raises: MissingParameterValue if required ipmi parameters are missing.
        :raises: IPMIFailure on an error from ipmitool.

        """
        if device not in self.get_supported_boot_devices(task):
            raise exception.InvalidParameterValue(_(
                "Invalid boot device %s specified.") % device)

        LOG.debug('Setting boot device to %(target)s requested for node '
                  '%(node)s with FX management',
                  {'target': device, 'node': task.node.uuid})

        if not persistent:
            LOG.warning('FX only supports persistent boot device setting')

        raw_cmd = '%s %s %s' % (
            FX_OEM_BOOTDEV_NETFN, FX_OEM_BOOTDEV_CMD_SET,
            FX_OEM_BOOTDEV_HEXA_MAP[device])
        ipmitool.send_raw(task, raw_cmd)

    @METRICS.timer('FxIPMIManagement.get_boot_device')
    def get_boot_device(self, task):
        """Get the current boot device for the task's node.

        Returns the current boot device of the node.

        :param task: a task from TaskManager.
        :raises: InvalidParameterValue if required IPMI parameters
            are missing.
        :raises: IPMIFailure on an error from ipmitool.
        :raises: MissingParameterValue if a required parameter is missing.
        :returns: a dictionary containing:

            :boot_device: the boot device, one of
                :mod:`ironic.common.boot_devices` or None if it is unknown.
            :persistent: Whether the boot device will persist to all
                future boots or not, None if it is unknown.

        """
        response = {'boot_device': None, 'persistent': True}

        raw_cmd = '%s %s' % (FX_OEM_BOOTDEV_NETFN, FX_OEM_BOOTDEV_CMD_GET)
        try:
            out, err = ipmitool.send_raw(task, raw_cmd)
        except exception.IPMIFailure as e:
            msg = ('Failed to get boot device for node %(node_id)s, '
                   'error: %(error)s' %
                   {'node_id': task.node.uuid, 'error': e})
            LOG.error(msg)
            raise exception.IPMIFailure(message=msg)

        boot_device = None
        for k, v in FX_OEM_BOOTDEV_HEXA_MAP.items():
            if v == out:
                boot_device = k
                break

        if boot_device is None:
            LOG.warning('Invalid boot device hexadecimal value: 0x%X', out)
        else:
            response['boot_device'] = boot_device

        return response

    def get_sensors_data(self, task):
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_sensors_data')

    def inject_nmi(self, task):
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='inject_nmi')
