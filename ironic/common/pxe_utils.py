#
# Copyright 2014 Rackspace, Inc
# All Rights Reserved
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

import os

from ironic_lib import utils as ironic_utils
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import fileutils

from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _
from ironic.common import image_service as service
from ironic.common import images
from ironic.common import states
from ironic.common import utils
from ironic.conf import CONF
from ironic.drivers.modules import boot_mode_utils
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules import image_cache
from ironic import objects

LOG = logging.getLogger(__name__)

PXE_CFG_DIR_NAME = CONF.pxe.pxe_config_subdir

DHCP_CLIENT_ID = '61'  # rfc2132
DHCP_TFTP_SERVER_NAME = '66'  # rfc2132
DHCP_BOOTFILE_NAME = '67'  # rfc2132
DHCPV6_BOOTFILE_NAME = '59'  # rfc5870
# NOTE(TheJulia): adding note for the bootfile parameter
# field as defined by RFC 5870. No practical examples seem
# available. Neither grub2 or ipxe seem to leverage this.
# DHCPV6_BOOTFILE_PARAMS = '60'  # rfc5870
DHCP_TFTP_SERVER_ADDRESS = '150'  # rfc5859
DHCP_IPXE_ENCAP_OPTS = '175'  # Tentatively Assigned
DHCP_TFTP_PATH_PREFIX = '210'  # rfc5071

DEPLOY_KERNEL_RAMDISK_LABELS = ['deploy_kernel', 'deploy_ramdisk']
RESCUE_KERNEL_RAMDISK_LABELS = ['rescue_kernel', 'rescue_ramdisk']
KERNEL_RAMDISK_LABELS = {'deploy': DEPLOY_KERNEL_RAMDISK_LABELS,
                         'rescue': RESCUE_KERNEL_RAMDISK_LABELS}


def get_root_dir():
    """Returns the directory where the config files and images will live."""
    return CONF.pxe.tftp_root


def get_ipxe_root_dir():
    return CONF.deploy.http_root


def _ensure_config_dirs_exist(task, ipxe_enabled=False):
    """Ensure that the node's and PXE configuration directories exist.

    :param task: A TaskManager instance
    :param ipxe_enabled: Default false boolean to indicate if ipxe
                         is in use by the caller.
    """
    if ipxe_enabled:
        root_dir = get_ipxe_root_dir()
    else:
        root_dir = get_root_dir()
    node_dir = os.path.join(root_dir, task.node.uuid)
    pxe_dir = os.path.join(root_dir, PXE_CFG_DIR_NAME)
    # NOTE: We should only change the permissions if the folder
    # does not exist. i.e. if defined, an operator could have
    # already created it and placed specific ACLs upon the folder
    # which may not recurse downward.
    for directory in (node_dir, pxe_dir):
        if not os.path.isdir(directory):
            fileutils.ensure_tree(directory)
            if CONF.pxe.dir_permission:
                os.chmod(directory, CONF.pxe.dir_permission)


def _link_mac_pxe_configs(task, ipxe_enabled=False):
    """Link each MAC address with the PXE configuration file.

    :param task: A TaskManager instance.
    :param ipxe_enabled: Default false boolean to indicate if ipxe
                         is in use by the caller.
    """

    def create_link(mac_path):
        ironic_utils.unlink_without_raise(mac_path)
        relative_source_path = os.path.relpath(
            pxe_config_file_path, os.path.dirname(mac_path))
        utils.create_link_without_raise(relative_source_path, mac_path)

    pxe_config_file_path = get_pxe_config_file_path(
        task.node.uuid, ipxe_enabled=ipxe_enabled)
    for port in task.ports:
        client_id = port.extra.get('client-id')
        # Syslinux, ipxe, depending on settings.
        create_link(_get_pxe_mac_path(port.address, client_id=client_id,
                                      ipxe_enabled=ipxe_enabled))
        # Grub2 MAC address only
        create_link(_get_pxe_grub_mac_path(port.address,
                                           ipxe_enabled=ipxe_enabled))


def _link_ip_address_pxe_configs(task, ipxe_enabled=False):
    """Link each IP address with the PXE configuration file.

    :param task: A TaskManager instance.
    :param ipxe_enabled: Default false boolean to indicate if ipxe
                         is in use by the caller.
    :raises: FailedToGetIPAddressOnPort
    :raises: InvalidIPv4Address

    """
    pxe_config_file_path = get_pxe_config_file_path(
        task.node.uuid,
        ipxe_enabled=ipxe_enabled)

    api = dhcp_factory.DHCPFactory().provider
    ip_addrs = api.get_ip_addresses(task)
    if not ip_addrs:

        if ip_addrs == []:
            LOG.warning("No IP addresses assigned for node %(node)s.",
                        {'node': task.node.uuid})
        else:
            LOG.warning(
                "DHCP address management is not available for node "
                "%(node)s. Operators without Neutron can ignore this "
                "warning.",
                {'node': task.node.uuid})
        # Just in case, reset to empty list if we got nothing.
        ip_addrs = []
    for port_ip_address in ip_addrs:
        ip_address_path = _get_pxe_ip_address_path(port_ip_address)
        ironic_utils.unlink_without_raise(ip_address_path)
        relative_source_path = os.path.relpath(
            pxe_config_file_path, os.path.dirname(ip_address_path))
        utils.create_link_without_raise(relative_source_path,
                                        ip_address_path)


def _get_pxe_grub_mac_path(mac, ipxe_enabled=False):
    root_dir = get_ipxe_root_dir() if ipxe_enabled else get_root_dir()
    return os.path.join(root_dir, mac + '.conf')


def _get_pxe_mac_path(mac, delimiter='-', client_id=None,
                      ipxe_enabled=False):
    """Convert a MAC address into a PXE config file name.

    :param mac: A MAC address string in the format xx:xx:xx:xx:xx:xx.
    :param delimiter: The MAC address delimiter. Defaults to dash ('-').
    :param client_id: client_id indicate InfiniBand port.
                      Defaults is None (Ethernet)
    :param ipxe_enabled: A default False boolean value to tell the method
                         if the caller is using iPXE.
    :returns: the path to the config file.

    """
    mac_file_name = mac.replace(':', delimiter).lower()
    if not ipxe_enabled:
        hw_type = '01-'
        if client_id:
            hw_type = '20-'
        mac_file_name = hw_type + mac_file_name
        return os.path.join(get_root_dir(), PXE_CFG_DIR_NAME,
                            mac_file_name)
    return os.path.join(get_ipxe_root_dir(), PXE_CFG_DIR_NAME,
                        mac_file_name)


def _get_pxe_ip_address_path(ip_address):
    """Convert an ipv4 address into a PXE config file name.

    :param ip_address: A valid IPv4 address string in the format 'n.n.n.n'.
    :returns: the path to the config file.

    """
    # grub2 bootloader needs ip based config file name.
    return os.path.join(
        CONF.pxe.tftp_root, ip_address + ".conf"
    )


def get_kernel_ramdisk_info(node_uuid, driver_info, mode='deploy',
                            ipxe_enabled=False):
    """Get href and tftp path for deploy or rescue kernel and ramdisk.

    :param node_uuid: UUID of the node
    :param driver_info: Node's driver_info dict
    :param mode: A label to indicate whether paths for deploy or rescue
                 ramdisk are being requested. Supported values are 'deploy'
                 'rescue'. Defaults to 'deploy', indicating deploy paths will
                 be returned.
    :param ipxe_enabled: A default False boolean value to tell the method
                         if the caller is using iPXE.
    :returns: a dictionary whose keys are deploy_kernel and deploy_ramdisk or
              rescue_kernel and rescue_ramdisk and whose values are the
              absolute paths to them.

    Note: driver_info should be validated outside of this method.
    """
    if ipxe_enabled:
        root_dir = get_ipxe_root_dir()
    else:
        root_dir = get_root_dir()
    image_info = {}
    labels = KERNEL_RAMDISK_LABELS[mode]
    for label in labels:
        image_info[label] = (
            str(driver_info[label]),
            os.path.join(root_dir, node_uuid, label)
        )
    return image_info


def get_pxe_config_file_path(node_uuid, ipxe_enabled=False):
    """Generate the path for the node's PXE configuration file.

    :param node_uuid: the UUID of the node.
    :param ipxe_enabled: A default False boolean value to tell the method
                         if the caller is using iPXE.
    :returns: The path to the node's PXE configuration file.

    """
    if ipxe_enabled:
        return os.path.join(get_ipxe_root_dir(), node_uuid, 'config')
    else:
        return os.path.join(get_root_dir(), node_uuid, 'config')


def create_pxe_config(task, pxe_options, template=None, ipxe_enabled=False):
    """Generate PXE configuration file and MAC address links for it.

    This method will generate the PXE configuration file for the task's
    node under a directory named with the UUID of that node. For each
    MAC address or DHCP IP address (port) of that node, a symlink for
    the configuration file will be created under the PXE configuration
    directory, so regardless of which port boots first they'll get the
    same PXE configuration.
    If grub2 bootloader is in use, then its configuration will be created
    based on DHCP IP address in the form nn.nn.nn.nn.

    :param task: A TaskManager instance.
    :param pxe_options: A dictionary with the PXE configuration
        parameters.
    :param template: The PXE configuration template. If no template is
        given the node specific template will be used.

    """
    LOG.debug("Building PXE config for node %s", task.node.uuid)
    if template is None:
        template = deploy_utils.get_pxe_config_template(task.node)

    _ensure_config_dirs_exist(task, ipxe_enabled)

    pxe_config_file_path = get_pxe_config_file_path(
        task.node.uuid,
        ipxe_enabled=ipxe_enabled)
    is_uefi_boot_mode = (boot_mode_utils.get_boot_mode(task.node)
                         == 'uefi')
    uefi_with_grub = is_uefi_boot_mode and not ipxe_enabled

    # grub bootloader panics with '{}' around any of its tags in its
    # config file. To overcome that 'ROOT' and 'DISK_IDENTIFIER' are enclosed
    # with '(' and ')' in uefi boot mode.
    if uefi_with_grub:
        pxe_config_root_tag = '(( ROOT ))'
        pxe_config_disk_ident = '(( DISK_IDENTIFIER ))'
    else:
        # TODO(stendulker): We should use '(' ')' as the delimiters for all our
        # config files so that we do not need special handling for each of the
        # bootloaders. Should be removed once the Mitaka release starts.
        pxe_config_root_tag = '{{ ROOT }}'
        pxe_config_disk_ident = '{{ DISK_IDENTIFIER }}'

    params = {'pxe_options': pxe_options,
              'ROOT': pxe_config_root_tag,
              'DISK_IDENTIFIER': pxe_config_disk_ident}

    pxe_config = utils.render_template(template, params)
    utils.write_to_file(pxe_config_file_path, pxe_config)

    # Always write the mac addresses
    _link_mac_pxe_configs(task, ipxe_enabled=ipxe_enabled)
    if uefi_with_grub:
        try:
            _link_ip_address_pxe_configs(task, ipxe_enabled)
        # NOTE(TheJulia): The IP address support will fail if the
        # dhcp_provider interface is set to none. This will result
        # in the MAC addresses and DHCP files being written, and
        # we can remove IP address creation for the grub use.
        except exception.FailedToGetIPAddressOnPort as e:
            if CONF.dhcp.dhcp_provider != 'none':
                with excutils.save_and_reraise_exception():
                    LOG.error('Unable to create boot config, IP address '
                              'was unable to be retrieved. %(error)s',
                              {'error': e})


def create_ipxe_boot_script():
    """Render the iPXE boot script into the HTTP root directory"""
    boot_script = utils.render_template(
        CONF.pxe.ipxe_boot_script,
        {'ipxe_for_mac_uri': PXE_CFG_DIR_NAME + '/'})
    bootfile_path = os.path.join(
        CONF.deploy.http_root,
        os.path.basename(CONF.pxe.ipxe_boot_script))
    # NOTE(pas-ha) to prevent unneeded writes,
    # only write to file if its content is different from required,
    # which should be rather rare
    if (not os.path.isfile(bootfile_path)
            or not utils.file_has_content(bootfile_path, boot_script)):
        utils.write_to_file(bootfile_path, boot_script)


def clean_up_pxe_config(task, ipxe_enabled=False):
    """Clean up the TFTP environment for the task's node.

    :param task: A TaskManager instance.

    """
    LOG.debug("Cleaning up PXE config for node %s", task.node.uuid)

    is_uefi_boot_mode = (boot_mode_utils.get_boot_mode(task.node) == 'uefi')

    if is_uefi_boot_mode and not ipxe_enabled:
        api = dhcp_factory.DHCPFactory().provider
        ip_addresses = api.get_ip_addresses(task)

        for port_ip_address in ip_addresses:
            try:
                # Get xx.xx.xx.xx based grub config file
                ip_address_path = _get_pxe_ip_address_path(port_ip_address)
            except exception.InvalidIPv4Address:
                continue
            except exception.FailedToGetIPAddressOnPort:
                continue
            # Cleaning up config files created for grub2.
            ironic_utils.unlink_without_raise(ip_address_path)

    for port in task.ports:
        client_id = port.extra.get('client-id')
        # syslinux, ipxe, etc.
        ironic_utils.unlink_without_raise(
            _get_pxe_mac_path(port.address, client_id=client_id,
                              ipxe_enabled=ipxe_enabled))
        # Grub2 MAC address based confiuration
        ironic_utils.unlink_without_raise(
            _get_pxe_grub_mac_path(port.address, ipxe_enabled=ipxe_enabled))
    if ipxe_enabled:
        utils.rmtree_without_raise(os.path.join(get_ipxe_root_dir(),
                                                task.node.uuid))
    else:
        utils.rmtree_without_raise(os.path.join(get_root_dir(),
                                                task.node.uuid))


def _dhcp_option_file_or_url(task, urlboot=False, ip_version=None):
    """Returns the appropriate file or URL.

    :param task: A TaskManager object.
    :param url_boot: Boolean value default False to indicate if a
                     URL should be returned to the file as opposed
                     to a file.
    :param ip_version: Integer representing the version of IP of
                       to return options for DHCP. Possible options
                       are 4, and 6.
    """
    boot_file = deploy_utils.get_pxe_boot_file(task.node)
    # NOTE(TheJulia): There are additional cases as we add new
    # features, so the logic below is in the form of if/elif/elif
    if not urlboot:
        return boot_file
    elif urlboot:
        if CONF.my_ipv6 and ip_version == 6:
            host = utils.wrap_ipv6(CONF.my_ipv6)
        else:
            host = utils.wrap_ipv6(CONF.pxe.tftp_server)
        return "tftp://{host}/{boot_file}".format(host=host,
                                                  boot_file=boot_file)


def dhcp_options_for_instance(task, ipxe_enabled=False, url_boot=False,
                              ip_version=None):
    """Retrieves the DHCP PXE boot options.

    :param task: A TaskManager instance.
    :param ipxe_enabled: Default false boolean that signals if iPXE
                         formatting should be returned by the method
                         for DHCP server configuration.
    :param url_boot: Default false boolean to inform the method if
                     a URL should be returned to boot the node.
                     If [pxe]ip_version is set to `6`, then this option
                     has no effect as url_boot form is required by DHCPv6
                     standards.
    :param ip_version: The IP version of options to return as values
                       differ by IP version. Default to [pxe]ip_version.
                       Possible options are integers 4 or 6.
    :returns: Dictionary to be sent to the networking service describing
              the DHCP options to be set.
    """
    if ip_version:
        use_ip_version = ip_version
    else:
        use_ip_version = int(CONF.pxe.ip_version)
    dhcp_opts = []
    dhcp_provider_name = CONF.dhcp.dhcp_provider
    if use_ip_version == 4:
        boot_file_param = DHCP_BOOTFILE_NAME
    else:
        # NOTE(TheJulia): Booting with v6 means it is always
        # a URL reply.
        boot_file_param = DHCPV6_BOOTFILE_NAME
        url_boot = True
    # NOTE(TheJulia): The ip_version value config from the PXE config is
    # guarded in the configuration, so there is no real sense in having
    # anything else here in the event the value is something aside from
    # 4 or 6, as there are no other possible values.
    boot_file = _dhcp_option_file_or_url(task, url_boot, use_ip_version)

    if ipxe_enabled:
        # TODO(TheJulia): DHCPv6 through dnsmasq + ipxe matching simply
        # does not work as the dhcp client is tracked via a different
        # identity mechanism in the exchange. This means if we really
        # want ipv6 + ipxe, we should be prepared to build a custom
        # iso with ipxe inside. Likely this is more secure and better
        # aligns with some of the mega-scale ironic operators.
        script_name = os.path.basename(CONF.pxe.ipxe_boot_script)
        # TODO(TheJulia): We should make this smarter to handle unwrapped v6
        # addresses, since the format is http://[ff80::1]:80/boot.ipxe.
        # As opposed to requiring configuration, we can eventually make this
        # dynamic, and would need to do similar then.
        ipxe_script_url = '/'.join([CONF.deploy.http_url, script_name])
        # if the request comes from dumb firmware send them the iPXE
        # boot image.
        if dhcp_provider_name == 'neutron':
            # Neutron use dnsmasq as default DHCP agent. Neutron carries the
            # configuration to relate to the tags below. The ipxe6 tag was
            # added in the Stein cycle which identifies the iPXE User-Class
            # directly and is only sent in DHCPv6.

            if use_ip_version != 6:
                dhcp_opts.append(
                    {'opt_name': "tag:!ipxe,%s" % boot_file_param,
                     'opt_value': boot_file}
                )
                dhcp_opts.append(
                    {'opt_name': "tag:ipxe,%s" % boot_file_param,
                     'opt_value': ipxe_script_url}
                )
            else:
                dhcp_opts.append(
                    {'opt_name': "tag:!ipxe6,%s" % boot_file_param,
                     'opt_value': boot_file})
                dhcp_opts.append(
                    {'opt_name': "tag:ipxe6,%s" % boot_file_param,
                     'opt_value': ipxe_script_url})
        else:
            # !175 == non-iPXE.
            # http://ipxe.org/howto/dhcpd#ipxe-specific_options
            if use_ip_version == 6:
                LOG.warning('IPv6 is enabled and the DHCP driver appears set '
                            'to a plugin aside from "neutron". Node %(name)s '
                            'may not receive proper DHCPv6 provided '
                            'boot parameters.', {'name': task.node.uuid})
            # NOTE(TheJulia): This was added for ISC DHCPd support, however it
            # appears that isc support was never added to neutron and is likely
            # a down stream driver.
            dhcp_opts.append({'opt_name': "!%s,%s" % (DHCP_IPXE_ENCAP_OPTS,
                              boot_file_param),
                              'opt_value': boot_file})
            dhcp_opts.append({'opt_name': boot_file_param,
                              'opt_value': ipxe_script_url})
    else:
        dhcp_opts.append({'opt_name': boot_file_param,
                          'opt_value': boot_file})
        # 210 == tftp server path-prefix or tftp root, will be used to find
        # pxelinux.cfg directory. The pxelinux.0 loader infers this information
        # from it's own path, but Petitboot needs it to be specified by this
        # option since it doesn't use pxelinux.0 loader.
        if not url_boot:
            dhcp_opts.append(
                {'opt_name': DHCP_TFTP_PATH_PREFIX,
                 'opt_value': get_tftp_path_prefix()})

    if not url_boot:
        dhcp_opts.append({'opt_name': DHCP_TFTP_SERVER_NAME,
                          'opt_value': CONF.pxe.tftp_server})
        dhcp_opts.append({'opt_name': DHCP_TFTP_SERVER_ADDRESS,
                          'opt_value': CONF.pxe.tftp_server})
    # NOTE(vsaienko) set this option specially for dnsmasq case as it always
    # sets `siaddr` field which is treated by pxe clients as TFTP server
    # see page 9 https://tools.ietf.org/html/rfc2131.
    # If `server-ip-address` is not provided dnsmasq sets `siaddr` to dnsmasq's
    # IP which breaks PXE booting as TFTP server is configured on ironic
    # conductor host.
    # http://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=blob;f=src/dhcp-common.c;h=eae9ae3567fe16eb979a484976c270396322efea;hb=a3303e196e5d304ec955c4d63afb923ade66c6e8#l572 # noqa
    # There is an informational RFC which describes how options related to
    # tftp 150,66 and siaddr should be used https://tools.ietf.org/html/rfc5859
    # All dhcp servers we've tried: contrail/dnsmasq/isc just silently ignore
    # unknown options but potentially it may blow up with others.
    # Related bug was opened on Neutron side:
    # https://bugs.launchpad.net/neutron/+bug/1723354
    if not url_boot:
        dhcp_opts.append({'opt_name': 'server-ip-address',
                          'opt_value': CONF.pxe.tftp_server})

    # Append the IP version for all the configuration options
    for opt in dhcp_opts:
        opt.update({'ip_version': use_ip_version})

    return dhcp_opts


def get_tftp_path_prefix():
    """Adds trailing slash (if needed) necessary for path-prefix

    :return: CONF.pxe.tftp_root ensured to have a trailing slash
    """
    return os.path.join(CONF.pxe.tftp_root, '')


def get_path_relative_to_tftp_root(file_path):
    """Return file relative path to CONF.pxe.tftp_root

    :param file_path: full file path to be made relative path.
    :returns: The path relative to CONF.pxe.tftp_root
    """
    return os.path.relpath(file_path, get_tftp_path_prefix())


def is_ipxe_enabled(task):
    """Return true if ipxe is set.

    :param task: A TaskManager object
    :returns: boolean true if ``[pxe]ipxe_enabled`` is configured
              or if the task driver instance is the iPXE driver.
    """
    return 'ipxe_boot' in task.driver.boot.capabilities


def parse_driver_info(node, mode='deploy'):
    """Gets the driver specific Node deployment info.

    This method validates whether the 'driver_info' property of the
    supplied node contains the required information for this driver to
    deploy images to, or rescue, the node.

    :param node: a single Node.
    :param mode: Label indicating a deploy or rescue operation being
                 carried out on the node. Supported values are
                 'deploy' and 'rescue'. Defaults to 'deploy', indicating
                 deploy operation is being carried out.
    :returns: A dict with the driver_info values.
    :raises: MissingParameterValue
    """
    info = node.driver_info

    params_to_check = KERNEL_RAMDISK_LABELS[mode]

    d_info = {k: info.get(k) for k in params_to_check}
    if not any(d_info.values()):
        # NOTE(dtantsur): avoid situation when e.g. deploy_kernel comes from
        # driver_info but deploy_ramdisk comes from configuration, since it's
        # a sign of a potential operator's mistake.
        d_info = {k: getattr(CONF.conductor, k) for k in params_to_check}
    error_msg = _("Cannot validate PXE bootloader. Some parameters were"
                  " missing in node's driver_info and configuration")
    deploy_utils.check_for_missing_params(d_info, error_msg)
    return d_info


def get_instance_image_info(task, ipxe_enabled=False):
    """Generate the paths for TFTP files for instance related images.

    This method generates the paths for instance kernel and
    instance ramdisk. This method also updates the node, so caller should
    already have a non-shared lock on the node.

    :param task: A TaskManager instance containing node and context.
    :param ipxe_enabled: Default false boolean to indicate if ipxe
                         is in use by the caller.
    :returns: a dictionary whose keys are the names of the images (kernel,
        ramdisk) and values are the absolute paths of them. If it's a whole
        disk image or node is configured for localboot,
        it returns an empty dictionary.
    """
    ctx = task.context
    node = task.node
    image_info = {}
    # NOTE(pas-ha) do not report image kernel and ramdisk for
    # local boot or whole disk images so that they are not cached
    if (node.driver_internal_info.get('is_whole_disk_image')
            or deploy_utils.get_boot_option(node) == 'local'):
        return image_info
    if ipxe_enabled:
        root_dir = get_ipxe_root_dir()
    else:
        root_dir = get_root_dir()
    i_info = node.instance_info
    labels = ('kernel', 'ramdisk')
    d_info = deploy_utils.get_image_instance_info(node)
    if not (i_info.get('kernel') and i_info.get('ramdisk')):
        glance_service = service.GlanceImageService(context=ctx)
        iproperties = glance_service.show(d_info['image_source'])['properties']
        for label in labels:
            i_info[label] = str(iproperties[label + '_id'])
        node.instance_info = i_info
        node.save()

    for label in labels:
        image_info[label] = (
            i_info[label],
            os.path.join(root_dir, node.uuid, label)
        )

    return image_info


def get_image_info(node, mode='deploy', ipxe_enabled=False):
    """Generate the paths for TFTP files for deploy or rescue images.

    This method generates the paths for the deploy (or rescue) kernel and
    deploy (or rescue) ramdisk.

    :param node: a node object
    :param mode: Label indicating a deploy or rescue operation being
        carried out on the node. Supported values are 'deploy' and 'rescue'.
        Defaults to 'deploy', indicating deploy operation is being carried out.
    :param ipxe_enabled: A default False boolean value to tell the method
                         if the caller is using iPXE.
    :returns: a dictionary whose keys are the names of the images
        (deploy_kernel, deploy_ramdisk, or rescue_kernel, rescue_ramdisk) and
        values are the absolute paths of them.
    :raises: MissingParameterValue, if deploy_kernel/deploy_ramdisk or
        rescue_kernel/rescue_ramdisk is missing in node's driver_info.
    """
    d_info = parse_driver_info(node, mode=mode)

    return get_kernel_ramdisk_info(
        node.uuid, d_info, mode=mode, ipxe_enabled=ipxe_enabled)


def build_deploy_pxe_options(task, pxe_info, mode='deploy',
                             ipxe_enabled=False):
    pxe_opts = {}
    node = task.node
    kernel_label = '%s_kernel' % mode
    ramdisk_label = '%s_ramdisk' % mode
    for label, option in ((kernel_label, 'deployment_aki_path'),
                          (ramdisk_label, 'deployment_ari_path')):
        if ipxe_enabled:
            image_href = pxe_info[label][0]
            if (CONF.pxe.ipxe_use_swift
                    and service_utils.is_glance_image(image_href)):
                pxe_opts[option] = images.get_temp_url_for_glance_image(
                    task.context, image_href)
            else:
                pxe_opts[option] = '/'.join([CONF.deploy.http_url, node.uuid,
                                            label])
        else:
            pxe_opts[option] = get_path_relative_to_tftp_root(
                pxe_info[label][1])
    if ipxe_enabled:
        pxe_opts['initrd_filename'] = ramdisk_label
    return pxe_opts


def build_instance_pxe_options(task, pxe_info, ipxe_enabled=False):
    pxe_opts = {}
    node = task.node

    for label, option in (('kernel', 'aki_path'),
                          ('ramdisk', 'ari_path')):
        if label in pxe_info:
            if ipxe_enabled:
                # NOTE(pas-ha) do not use Swift TempURLs for kernel and
                # ramdisk of user image when boot_option is not local,
                # as this breaks instance reboot later when temp urls
                # have timed out.
                pxe_opts[option] = '/'.join(
                    [CONF.deploy.http_url, node.uuid, label])
            else:
                # It is possible that we don't have kernel/ramdisk or even
                # image_source to determine if it's a whole disk image or not.
                # For example, when transitioning to 'available' state
                # for first time from 'manage' state.
                pxe_opts[option] = get_path_relative_to_tftp_root(
                    pxe_info[label][1])

    pxe_opts.setdefault('aki_path', 'no_kernel')
    pxe_opts.setdefault('ari_path', 'no_ramdisk')

    i_info = task.node.instance_info
    try:
        pxe_opts['ramdisk_opts'] = i_info['ramdisk_kernel_arguments']
    except KeyError:
        pass

    return pxe_opts


def build_extra_pxe_options(ramdisk_params=None):
    # Enable debug in IPA according to CONF.debug if it was not
    # specified yet
    pxe_append_params = CONF.pxe.pxe_append_params
    if CONF.debug and 'ipa-debug' not in pxe_append_params:
        pxe_append_params += ' ipa-debug=1'
    if ramdisk_params:
        pxe_append_params += ' ' + ' '.join('%s=%s' % tpl
                                            for tpl in ramdisk_params.items())

    return {'pxe_append_params': pxe_append_params,
            'tftp_server': CONF.pxe.tftp_server,
            'ipxe_timeout': CONF.pxe.ipxe_timeout * 1000}


def build_pxe_config_options(task, pxe_info, service=False,
                             ipxe_enabled=False, ramdisk_params=None):
    """Build the PXE config options for a node

    This method builds the PXE boot options for a node,
    given all the required parameters.

    The options should then be passed to pxe_utils.create_pxe_config to
    create the actual config files.

    :param task: A TaskManager object
    :param pxe_info: a dict of values to set on the configuration file
    :param service: if True, build "service mode" pxe config for netboot-ed
        user image and skip adding deployment image kernel and ramdisk info
        to PXE options.
    :param ipxe_enabled: Default false boolean to indicate if ipxe
                         is in use by the caller.
    :param ramdisk_params: the parameters to be passed to the ramdisk.
                           as kernel command-line arguments.
    :returns: A dictionary of pxe options to be used in the pxe bootfile
        template.
    """
    node = task.node
    mode = deploy_utils.rescue_or_deploy_mode(node)
    if service:
        pxe_options = {}
    elif node.driver_internal_info.get('boot_from_volume'):
        pxe_options = get_volume_pxe_options(task)
    else:
        pxe_options = build_deploy_pxe_options(task, pxe_info, mode=mode,
                                               ipxe_enabled=ipxe_enabled)

    # NOTE(pas-ha) we still must always add user image kernel and ramdisk
    # info as later during switching PXE config to service mode the
    # template will not be regenerated anew, but instead edited as-is.
    # This can be changed later if/when switching PXE config will also use
    # proper templating instead of editing existing files on disk.
    pxe_options.update(build_instance_pxe_options(task, pxe_info,
                                                  ipxe_enabled=ipxe_enabled))

    pxe_options.update(build_extra_pxe_options(ramdisk_params))

    return pxe_options


def build_service_pxe_config(task, instance_image_info,
                             root_uuid_or_disk_id,
                             ramdisk_boot=False,
                             ipxe_enabled=False):
    node = task.node
    pxe_config_path = get_pxe_config_file_path(node.uuid,
                                               ipxe_enabled=ipxe_enabled)
    # NOTE(pas-ha) if it is takeover of ACTIVE node or node performing
    # unrescue operation, first ensure that basic PXE configs and links
    # are in place before switching pxe config
    # NOTE(TheJulia): Also consider deploying a valid state to go ahead
    # and check things before continuing, as otherwise deployments can
    # fail if the agent was booted outside the direct actions of the
    # boot interface.
    if (node.provision_state in [states.ACTIVE, states.UNRESCUING,
                                 states.DEPLOYING]
            and not os.path.isfile(pxe_config_path)):
        pxe_options = build_pxe_config_options(task, instance_image_info,
                                               service=True,
                                               ipxe_enabled=ipxe_enabled)
        pxe_config_template = deploy_utils.get_pxe_config_template(node)
        create_pxe_config(task, pxe_options, pxe_config_template,
                          ipxe_enabled=ipxe_enabled)
    iwdi = node.driver_internal_info.get('is_whole_disk_image')

    deploy_utils.switch_pxe_config(
        pxe_config_path, root_uuid_or_disk_id,
        boot_mode_utils.get_boot_mode(node),
        iwdi, deploy_utils.is_trusted_boot_requested(node),
        deploy_utils.is_iscsi_boot(task), ramdisk_boot,
        ipxe_enabled=ipxe_enabled)


def get_volume_pxe_options(task):
    """Identify volume information for iPXE template generation."""
    def __return_item_or_first_if_list(item):
        if isinstance(item, list):
            return item[0]
        else:
            return item

    def __get_property(properties, key):
        prop = __return_item_or_first_if_list(properties.get(key, ''))
        if prop != '':
            return prop
        return __return_item_or_first_if_list(properties.get(key + 's', ''))

    def __generate_iscsi_url(properties):
        """Returns iscsi url."""
        portal = __get_property(properties, 'target_portal')
        iqn = __get_property(properties, 'target_iqn')
        lun = __get_property(properties, 'target_lun')

        if ':' in portal:
            host, port = portal.split(':')
        else:
            host = portal
            port = ''
        return ("iscsi:%(host)s::%(port)s:%(lun)s:%(iqn)s" %
                {'host': host, 'port': port, 'lun': lun, 'iqn': iqn})

    pxe_options = {}
    node = task.node
    boot_volume = node.driver_internal_info.get('boot_from_volume')
    volume = objects.VolumeTarget.get_by_uuid(task.context,
                                              boot_volume)

    properties = volume.properties
    if 'iscsi' in volume['volume_type']:
        if 'auth_username' in properties:
            pxe_options['username'] = properties['auth_username']
        if 'auth_password' in properties:
            pxe_options['password'] = properties['auth_password']
        iscsi_initiator_iqn = None
        for vc in task.volume_connectors:
            if vc.type == 'iqn':
                iscsi_initiator_iqn = vc.connector_id

        pxe_options.update(
            {'iscsi_boot_url': __generate_iscsi_url(volume.properties),
             'iscsi_initiator_iqn': iscsi_initiator_iqn})
        # NOTE(TheJulia): This may be the route to multi-path, define
        # volumes via sanhook in the ipxe template and let the OS sort it out.
        extra_targets = []

        for target in task.volume_targets:
            if target.boot_index != 0 and 'iscsi' in target.volume_type:
                iscsi_url = __generate_iscsi_url(target.properties)
                username = target.properties['auth_username']
                password = target.properties['auth_password']
                extra_targets.append({'url': iscsi_url,
                                      'username': username,
                                      'password': password})
        pxe_options.update({'iscsi_volumes': extra_targets,
                            'boot_from_volume': True})
    # TODO(TheJulia): FibreChannel boot, i.e. wwpn in volume_type
    # for FCoE, should go here.
    return pxe_options


def validate_boot_parameters_for_trusted_boot(node):
    """Check if boot parameters are valid for trusted boot."""
    boot_mode = boot_mode_utils.get_boot_mode(node)
    boot_option = deploy_utils.get_boot_option(node)
    is_whole_disk_image = node.driver_internal_info.get('is_whole_disk_image')
    # 'is_whole_disk_image' is not supported by trusted boot, because there is
    # no Kernel/Ramdisk to measure at all.
    if (boot_mode != 'bios'
        or is_whole_disk_image
        or boot_option != 'netboot'):
        msg = (_("Trusted boot is only supported in BIOS boot mode with "
                 "netboot and without whole_disk_image, but Node "
                 "%(node_uuid)s was configured with boot_mode: %(boot_mode)s, "
                 "boot_option: %(boot_option)s, is_whole_disk_image: "
                 "%(is_whole_disk_image)s: at least one of them is wrong, and "
                 "this can be caused by enable secure boot.") %
               {'node_uuid': node.uuid, 'boot_mode': boot_mode,
                'boot_option': boot_option,
                'is_whole_disk_image': is_whole_disk_image})
        LOG.error(msg)
        raise exception.InvalidParameterValue(msg)


def prepare_instance_pxe_config(task, image_info,
                                iscsi_boot=False,
                                ramdisk_boot=False,
                                ipxe_enabled=False):
    """Prepares the config file for PXE boot

    :param task: a task from TaskManager.
    :param image_info: a dict of values of instance image
                       metadata to set on the configuration file.
    :param iscsi_boot: if boot is from an iSCSI volume or not.
    :param ramdisk_boot: if the boot is to a ramdisk configuration.
    :param ipxe_enabled: Default false boolean to indicate if ipxe
                         is in use by the caller.
    :returns: None
    """

    node = task.node
    # Generate options for both IPv4 and IPv6, and they can be
    # filtered down later based upon the port options.
    # TODO(TheJulia): This should be re-tooled during the Victoria
    # development cycle so that we call a single method and return
    # combined options. The method we currently call is relied upon
    # by two eternal projects, to changing the behavior is not ideal.
    dhcp_opts = dhcp_options_for_instance(task, ipxe_enabled,
                                          ip_version=4)
    dhcp_opts += dhcp_options_for_instance(task, ipxe_enabled,
                                           ip_version=6)
    provider = dhcp_factory.DHCPFactory()
    provider.update_dhcp(task, dhcp_opts)
    pxe_config_path = get_pxe_config_file_path(
        node.uuid, ipxe_enabled=ipxe_enabled)
    if not os.path.isfile(pxe_config_path):
        pxe_options = build_pxe_config_options(
            task, image_info, service=ramdisk_boot,
            ipxe_enabled=ipxe_enabled)
        pxe_config_template = (
            deploy_utils.get_pxe_config_template(node))
        create_pxe_config(
            task, pxe_options, pxe_config_template,
            ipxe_enabled=ipxe_enabled)
    deploy_utils.switch_pxe_config(
        pxe_config_path, None,
        boot_mode_utils.get_boot_mode(node), False,
        iscsi_boot=iscsi_boot, ramdisk_boot=ramdisk_boot,
        ipxe_enabled=ipxe_enabled)


@image_cache.cleanup(priority=25)
class TFTPImageCache(image_cache.ImageCache):
    def __init__(self):
        master_path = CONF.pxe.tftp_master_path or None
        super(TFTPImageCache, self).__init__(
            master_path,
            # MiB -> B
            cache_size=CONF.pxe.image_cache_size * 1024 * 1024,
            # min -> sec
            cache_ttl=CONF.pxe.image_cache_ttl * 60)


def cache_ramdisk_kernel(task, pxe_info, ipxe_enabled=False):
    """Fetch the necessary kernels and ramdisks for the instance."""
    ctx = task.context
    node = task.node
    if ipxe_enabled:
        path = os.path.join(get_ipxe_root_dir(), node.uuid)
    else:
        path = os.path.join(get_root_dir(), node.uuid)
    fileutils.ensure_tree(path)
    LOG.debug("Fetching necessary kernel and ramdisk for node %s",
              node.uuid)
    deploy_utils.fetch_images(ctx, TFTPImageCache(), list(pxe_info.values()),
                              CONF.force_raw_images)


def clean_up_pxe_env(task, images_info, ipxe_enabled=False):
    """Cleanup PXE environment of all the images in images_info.

    Cleans up the PXE environment for the mentioned images in
    images_info.

    :param task: a TaskManager object
    :param images_info: A dictionary of images whose keys are the image names
        to be cleaned up (kernel, ramdisk, etc) and values are a tuple of
        identifier and absolute path.
    """
    for label in images_info:
        path = images_info[label][1]
        ironic_utils.unlink_without_raise(path)

    clean_up_pxe_config(task, ipxe_enabled=ipxe_enabled)
    TFTPImageCache().clean_up()
