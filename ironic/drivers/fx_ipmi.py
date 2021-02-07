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

from ironic.drivers import generic
from ironic.drivers.modules.fx_ipmi import management
from ironic.drivers.modules import ipmitool
from ironic.drivers.modules import noop_mgmt


class FxIPMIHardware(generic.GenericHardware):
    """FUJITSU Supercomputer PRIMEHPC FX700 IPMI hardware type.

    Fx700 only supports standard IPMI power management cmds and OEM IPMI
    boot devices management cmds[1].

    For serial console it use ssh not IPMI sol to connect to it[2]. Serial
    console interface will be implemented in the future maybe.
    [1]: "Chapter 4 Command Support (IPMI)" of
         https://www.fujitsu.com/downloads/SUPER/manual/c120-0091-05en.pdf
    [2]: "Chapter 4.6.1 Connecting to the Console" of
         https://www.fujitsu.com/downloads/SUPER/manual/c120-0090-04en.pdf
    """
    @property
    def supported_management_interfaces(self):
        """List of supported management interfaces."""
        return [management.FxIPMIManagement, noop_mgmt.NoopManagement]

    @property
    def supported_power_interfaces(self):
        """List of supported power interfaces."""
        return [ipmitool.IPMIPower]
