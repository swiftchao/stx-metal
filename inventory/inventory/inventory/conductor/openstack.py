#
# Copyright (c) 2013-2018 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

# vim: tabstop=4 shiftwidth=4 softtabstop=4

# All Rights Reserved.
#

""" Inventory Openstack Utilities and helper functions."""

from barbicanclient.v1 import client as barbican_client_v1
from cinderclient.v2 import client as cinder_client_v2
from inventory.common import constants
from inventory.common import exception
from inventory.common.i18n import _
from inventory.common.storage_backend_conf import StorageBackendConfig
from keystoneclient.auth.identity import v3
from keystoneclient import exceptions as identity_exc
from keystoneclient import session
from keystoneclient.v3 import client as keystone_client
from neutronclient.v2_0 import client as neutron_client_v2_0
from novaclient.v2 import client as nova_client_v2
from oslo_config import cfg
from oslo_log import log
from sqlalchemy.orm import exc


LOG = log.getLogger(__name__)

keystone_opts = [
    cfg.StrOpt('auth_host',
               default='controller',
               help=_("Authentication host server")),
    cfg.IntOpt('auth_port',
               default=5000,
               help=_("Authentication host port number")),
    cfg.StrOpt('auth_protocol',
               default='http',
               help=_("Authentication protocol")),
    cfg.StrOpt('admin_user',
               default='admin',
               help=_("Admin user")),
    cfg.StrOpt('admin_password',
               default='admin',   # this is usually some value
               help=_("Admin password"),
               secret=True),
    cfg.StrOpt('admin_tenant_name',
               default='services',
               help=_("Admin tenant name")),
    cfg.StrOpt('auth_uri',
               default='http://192.168.204.2:5000/',
               help=_("Authentication URI")),
    cfg.StrOpt('auth_url',
               default='http://127.0.0.1:5000/',
               help=_("Admin Authentication URI")),
    cfg.StrOpt('region_name',
               default='RegionOne',
               help=_("Region Name")),
    cfg.StrOpt('neutron_region_name',
               default='RegionOne',
               help=_("Neutron Region Name")),
    cfg.StrOpt('cinder_region_name',
               default='RegionOne',
               help=_("Cinder Region Name")),
    cfg.StrOpt('nova_region_name',
               default='RegionOne',
               help=_("Nova Region Name")),
    cfg.StrOpt('barbican_region_name',
               default='RegionOne',
               help=_("Barbican Region Name")),
    cfg.StrOpt('username',
               default='inventory',
               help=_("Inventory keystone user name")),
    cfg.StrOpt('password',
               default='inventory',
               help=_("Inventory keystone user password")),
    cfg.StrOpt('project_name',
               default='services',
               help=_("Inventory keystone user project name")),
    cfg.StrOpt('user_domain_name',
               default='Default',
               help=_("Inventory keystone user domain name")),
    cfg.StrOpt('project_domain_name',
               default='Default',
               help=_("Inventory keystone user project domain name"))
]

# Register the configuration options
cfg.CONF.register_opts(keystone_opts, "KEYSTONE_AUTHTOKEN")


class OpenStackOperator(object):
    """Class to encapsulate OpenStack operations for Inventory"""

    def __init__(self, dbapi):
        self.dbapi = dbapi
        self.barbican_client = None
        self.cinder_client = None
        self.keystone_client = None
        self.keystone_session = None
        self.nova_client = None
        self.neutron_client = None
        self._neutron_extension_list = []
        self.auth_url = cfg.CONF.KEYSTONE_AUTHTOKEN.auth_url + "/v3"

    #################
    # NEUTRON
    #################

    def _get_neutronclient(self):
        if not self.neutron_client:  # should not cache this forever
            # neutronclient doesn't yet support v3 keystone auth
            # use keystoneauth.session
            self.neutron_client = neutron_client_v2_0.Client(
                session=self._get_keystone_session(),
                auth_url=self.auth_url,
                endpoint_type='internalURL',
                region_name=cfg.CONF.KEYSTONE_AUTHTOKEN.neutron_region_name)
        return self.neutron_client

    def get_providernetworksdict(self, pn_names=None, quiet=False):
        """
        Returns names and MTU values of neutron's providernetworks
        """
        pn_dict = {}

        # Call neutron
        try:
            pn_list = self._get_neutronclient().list_providernets().get(
                'providernets', [])
        except Exception as e:
            if not quiet:
                LOG.error("Failed to access Neutron client")
                LOG.error(e)
            return pn_dict

        # Get dict
        # If no names specified, will add all providenets to dict
        for pn in pn_list:
            if pn_names and pn['name'] not in pn_names:
                continue
            else:
                pn_dict.update({pn['name']: pn})

        return pn_dict

    def neutron_extension_list(self, context):
        """
        Send a request to neutron to query the supported extension list.
        """
        if not self._neutron_extension_list:
            client = self._get_neutronclient()
            extensions = client.list_extensions().get('extensions', [])
            self._neutron_extension_list = [e['alias'] for e in extensions]
        return self._neutron_extension_list

    def bind_interface(self, context, host_uuid, interface_uuid,
                       network_type, providernets, mtu,
                       vlans=None, test=False):
        """
        Send a request to neutron to bind an interface to a set of provider
        networks, and inform neutron of some key attributes of the interface
        for semantic checking purposes.

        Any remote exceptions from neutron are allowed to pass-through and are
        expected to be handled by the caller.
        """
        client = self._get_neutronclient()
        body = {'interface': {'uuid': interface_uuid,
                              'providernets': providernets,
                              'network_type': network_type,
                              'mtu': mtu}}
        if vlans:
            body['interface']['vlans'] = vlans
        if test:
            body['interface']['test'] = True
        client.host_bind_interface(host_uuid, body=body)
        return True

    def unbind_interface(self, context, host_uuid, interface_uuid):
        """
        Send a request to neutron to unbind an interface from a set of
        provider networks.

        Any remote exceptions from neutron are allowed to pass-through and are
        expected to be handled by the caller.
        """
        client = self._get_neutronclient()
        body = {'interface': {'uuid': interface_uuid}}
        client.host_unbind_interface(host_uuid, body=body)
        return True

    def get_neutron_host_id_by_name(self, context, name):
        """
        Get a neutron host
        """

        client = self._get_neutronclient()

        hosts = client.list_hosts()

        if not hosts:
            return ""

        for host in hosts['hosts']:
            if host['name'] == name:
                return host['id']

        return ""

    def create_neutron_host(self, context, host_uuid, name,
                            availability='down'):
        """
        Send a request to neutron to create a host
        """
        client = self._get_neutronclient()
        body = {'host': {'id': host_uuid,
                         'name': name,
                         'availability': availability
                         }}
        client.create_host(body=body)
        return True

    def delete_neutron_host(self, context, host_uuid):
        """
        Delete a neutron host
        """
        client = self._get_neutronclient()

        client.delete_host(host_uuid)

        return True

    #################
    # NOVA
    #################

    def _get_novaclient(self):
        if not self.nova_client:  # should not cache this forever
            # novaclient doesn't yet support v3 keystone auth
            # use keystoneauth.session
            self.nova_client = nova_client_v2.Client(
                session=self._get_keystone_session(),
                auth_url=self.auth_url,
                endpoint_type='internalURL',
                direct_use=False,
                region_name=cfg.CONF.KEYSTONE_AUTHTOKEN.nova_region_name)
        return self.nova_client

    def try_interface_get_by_host(self, host_uuid):
        try:
            interfaces = self.dbapi.iinterface_get_by_ihost(host_uuid)
        except exc.DetachedInstanceError:
            # A rare DetachedInstanceError exception may occur, retry
            LOG.exception("Detached Instance Error,  retry "
                          "iinterface_get_by_ihost %s" % host_uuid)
            interfaces = self.dbapi.iinterface_get_by_ihost(host_uuid)

        return interfaces

    def nova_host_available(self, ihost_uuid):
        """
        Perform inventory driven nova operations for an available ihost
        """
        # novaclient/v3
        #
        # # On unlock, check whether exists:
        # 1. nova aggregate-create provider_physnet0 nova
        #    cs.aggregates.create(args.name, args.availability_zone)
        #    e.g.          create(provider_physnet0,  None)
        #
        #    can query it from do_aggregate_list
        #        ('Name', 'Availability Zone'); anyways it doesnt
        #    allow duplicates on Name. can be done prior to compute nodes?
        #
        # # On unlock, check whether exists: metadata is a key/value pair
        # 2. nova aggregate-set-metadata provider_physnet0 \
        #                      provider:physical_network=physnet0
        #    aggregate = _find_aggregate(cs, args.aggregate)
        #    metadata = _extract_metadata(args)
        #    cs.aggregates.set_metadata(aggregate.id, metadata)
        #
        #    This can be run mutliple times regardless.
        #
        # 3. nova aggregate-add-host provider_physnet0 compute-0
        #    cs.aggregates.add_host(aggregate.id, args.host)
        #
        #    Can only be after nova knows about this resource!!!
        #    Doesnt allow duplicates,therefore agent must trigger conductor
        #    to perform the function. A single sync call upon init.
        #    On every unlock try for about 5 minutes? or check admin state
        #    and skip it. it needs to try several time though or needs to
        #    know that nova is up and running before sending it.
        #    e.g. agent audit look for and transitions
        #         /etc/platform/.initial_config_complete
        #         however, it needs to do this on every unlock may update
        #
        # Remove aggregates from provider network - on delete of host.
        # 4. nova aggregate-remove-host provider_physnet0 compute-0
        #    cs.aggregates.remove_host(aggregate.id, args.host)
        #
        # Do we ever need to do this?
        # 5. nova aggregate-delete provider_physnet0
        #    cs.aggregates.delete(aggregate)
        #
        # report to nova host aggregate groupings once node is available

        availability_zone = None
        aggregate_name_prefix = 'provider_'
        ihost_providernets = []

        ihost_aggset_provider = set()
        nova_aggset_provider = set()

        # determine which providernets are on this ihost
        try:
            iinterfaces = self.try_interface_get_by_host(ihost_uuid)
            for interface in iinterfaces:
                networktypelist = []
                if interface.networktype:
                    networktypelist = [
                        network.strip()
                        for network in interface['networktype'].split(",")]
                if constants.NETWORK_TYPE_DATA in networktypelist:
                    providernets = interface.providernetworks
                    for providernet in providernets.split(',') \
                            if providernets else []:
                        ihost_aggset_provider.add(aggregate_name_prefix +
                                                  providernet)

            ihost_providernets = list(ihost_aggset_provider)
        except Exception:
            LOG.exception("AGG iinterfaces_get failed for %s." % ihost_uuid)

        try:
            aggregates = self._get_novaclient().aggregates.list()
        except Exception:
            self.nova_client = None  # password may have updated
            aggregates = self._get_novaclient().aggregates.list()
            pass

        for aggregate in aggregates:
            nova_aggset_provider.add(aggregate.name)

        if ihost_providernets:
            agglist_missing = \
                list(ihost_aggset_provider - nova_aggset_provider)
            LOG.debug("AGG agglist_missing = %s." % agglist_missing)

            for i in agglist_missing:
                # 1. nova aggregate-create provider_physnet0
                # use None for the availability zone
                #    cs.aggregates.create(args.name, args.availability_zone)
                try:
                    aggregate = self._get_novaclient().aggregates.create(
                        i, availability_zone)
                    aggregates.append(aggregate)
                    LOG.debug("AGG6 aggregate= %s. aggregates= %s" %
                              (aggregate, aggregates))
                except Exception:
                    # do not continue i, redo as potential race condition
                    LOG.error("AGG6 EXCEPTION aggregate i=%s, aggregates=%s" %
                              (i, aggregates))

                    # let it try again, so it can rebuild the aggregates list
                    return False

                # 2. nova aggregate-set-metadata provider_physnet0 \
                #                                provider:physical_network=physnet0
                #    aggregate = _find_aggregate(cs, args.aggregate)
                #    metadata = _extract_metadata(args)
                #    cs.aggregates.set_metadata(aggregate.id, metadata)
                try:
                    metadata = {}
                    key = 'provider:physical_network'
                    metadata[key] = i[9:]

                    # pre-check: only add/modify if aggregate is valid
                    if aggregate_name_prefix + metadata[key] == aggregate.name:
                        LOG.debug("AGG8 aggregate metadata = %s." % metadata)
                        aggregate = \
                            self._get_novaclient().aggregates.set_metadata(
                                aggregate.id, metadata)
                except Exception:
                    LOG.error("AGG8 EXCEPTION aggregate")
                    pass

            # 3. nova aggregate-add-host provider_physnet0 compute-0
            #    cs.aggregates.add_host(aggregate.id, args.host)

            #  aggregates = self._get_novaclient().aggregates.list()
            ihost = self.dbapi.ihost_get(ihost_uuid)

            for i in aggregates:
                if i.name in ihost_providernets:
                    metadata = self._get_novaclient().aggregates.get(int(i.id))

                    nhosts = []
                    if hasattr(metadata, 'hosts'):
                        nhosts = metadata.hosts or []

                    if ihost.hostname in nhosts:
                        LOG.warn("host=%s in already in aggregate id=%s" %
                                 (ihost.hostname, i.id))
                    else:
                        try:
                            metadata = \
                                self._get_novaclient().aggregates.add_host(
                                    i.id, ihost.hostname)
                        except Exception:
                            LOG.warn("AGG10 EXCEPTION aggregate id = %s "
                                     "ihost= %s."
                                     % (i.id, ihost.hostname))
                            return False
        else:
            LOG.warn("AGG ihost_providernets empty %s." % ihost_uuid)

    def nova_host_offline(self, ihost_uuid):
        """
        Perform inventory driven nova operations for an unavailable ihost,
        such as may occur when a host is locked, since if providers
        may change before being unlocked again.
        """
        # novaclient/v3
        #
        # # On delete, check whether exists:
        #
        # Remove aggregates from provider network - on delete of host.
        # 4. nova aggregate-remove-host provider_physnet0 compute-0
        #    cs.aggregates.remove_host(aggregate.id, args.host)
        #
        # Do we ever need to do this?
        # 5. nova aggregate-delete provider_physnet0
        #    cs.aggregates.delete(aggregate)
        #

        aggregate_name_prefix = 'provider_'
        ihost_providernets = []

        ihost_aggset_provider = set()
        nova_aggset_provider = set()

        # determine which providernets are on this ihost
        try:
            iinterfaces = self.try_interface_get_by_host(ihost_uuid)
            for interface in iinterfaces:
                networktypelist = []
                if interface.networktype:
                    networktypelist = [network.strip() for network in
                                       interface['networktype'].split(",")]
                if constants.NETWORK_TYPE_DATA in networktypelist:
                    providernets = interface.providernetworks
                    for providernet in (
                            providernets.split(',') if providernets else []):
                        ihost_aggset_provider.add(aggregate_name_prefix +
                                                  providernet)
            ihost_providernets = list(ihost_aggset_provider)
        except Exception:
            LOG.exception("AGG iinterfaces_get failed for %s." % ihost_uuid)

        try:
            aggregates = self._get_novaclient().aggregates.list()
        except Exception:
            self.nova_client = None  # password may have updated
            aggregates = self._get_novaclient().aggregates.list()

        if ihost_providernets:
            for aggregate in aggregates:
                nova_aggset_provider.add(aggregate.name)
        else:
            LOG.debug("AGG ihost_providernets empty %s." % ihost_uuid)

        # Remove aggregates from provider network. Anything with host in list.
        # 4. nova aggregate-remove-host provider_physnet0 compute-0
        #    cs.aggregates.remove_host(aggregate.id, args.host)

        ihost = self.dbapi.ihost_get(ihost_uuid)

        for aggregate in aggregates:
            try:
                LOG.debug("AGG10 remove aggregate id = %s ihost= %s." %
                          (aggregate.id, ihost.hostname))
                self._get_novaclient().aggregates.remove_host(
                    aggregate.id, ihost.hostname)
            except Exception:
                LOG.debug("AGG10 EXCEPTION remove aggregate")
                pass

        return True

    #################
    # Keystone
    #################
    def _get_keystone_session(self):
        if not self.keystone_session:
            auth = v3.Password(auth_url=self.auth_url,
                               username=cfg.CONF.KEYSTONE_AUTHTOKEN.username,
                               password=cfg.CONF.KEYSTONE_AUTHTOKEN.password,
                               user_domain_name=cfg.CONF.KEYSTONE_AUTHTOKEN.
                               user_domain_name,
                               project_name=cfg.CONF.KEYSTONE_AUTHTOKEN.
                               project_name,
                               project_domain_name=cfg.CONF.KEYSTONE_AUTHTOKEN.
                               project_domain_name)
            self.keystone_session = session.Session(auth=auth)
        return self.keystone_session

    def _get_keystoneclient(self):
        if not self.keystone_client:  # should not cache this forever
            self.keystone_client = keystone_client.Client(
                username=cfg.CONF.KEYSTONE_AUTHTOKEN.username,
                user_domain_name=cfg.CONF.KEYSTONE_AUTHTOKEN.user_domain_name,
                project_name=cfg.CONF.KEYSTONE_AUTHTOKEN.project_name,
                project_domain_name=cfg.CONF.KEYSTONE_AUTHTOKEN
                .project_domain_name,
                password=cfg.CONF.KEYSTONE_AUTHTOKEN.password,
                auth_url=self.auth_url,
                region_name=cfg.CONF.KEYSTONE_AUTHTOKEN.region_name)
        return self.keystone_client

    def _get_identity_id(self):
        try:
            LOG.debug("Search service id for : (%s)" %
                      constants.SERVICE_TYPE_IDENTITY)
            service = self._get_keystoneclient().services.find(
                type=constants.SERVICE_TYPE_IDENTITY)
        except identity_exc.NotFound:
            LOG.error("Could not find service id for (%s)" %
                      constants.SERVICE_TYPE_IDENTITY)
            return None
        except identity_exc.NoUniqueMatch:
            LOG.error("Multiple service matches found for (%s)" %
                      constants.SERVICE_TYPE_IDENTITY)
            return None
        return service.id

    #################
    # Cinder
    #################
    def _get_cinder_endpoints(self):
        endpoint_list = []
        try:
            # get region one name from platform.conf
            region1_name = get_region_name('region_1_name')
            if region1_name is None:
                region1_name = 'RegionOne'
            service_list = self._get_keystoneclient().services.list()
            for s in service_list:
                if s.name.find(constants.SERVICE_TYPE_CINDER) != -1:
                    endpoint_list += self._get_keystoneclient().endpoints.list(
                        service=s, region=region1_name)
        except Exception:
            LOG.error("Failed to get keystone endpoints for cinder.")
        return endpoint_list

    def _get_cinderclient(self):
        if not self.cinder_client:
            self.cinder_client = cinder_client_v2.Client(
                session=self._get_keystone_session(),
                auth_url=self.auth_url,
                endpoint_type='internalURL',
                region_name=cfg.CONF.KEYSTONE_AUTHTOKEN.cinder_region_name)

        return self.cinder_client

    def get_cinder_pools(self):
        pools = {}

        # Check to see if cinder is present
        # TODO(rchurch): Need to refactor with storage backend
        if ((StorageBackendConfig.has_backend_configured(
                self.dbapi, constants.CINDER_BACKEND_CEPH)) or
                (StorageBackendConfig.has_backend_configured(
                    self.dbapi, constants.CINDER_BACKEND_LVM))):
            try:
                pools = self._get_cinderclient().pools.list(detailed=True)
            except Exception as e:
                LOG.error("get_cinder_pools: Failed to access "
                          "Cinder client: %s" % e)

        return pools

    def get_cinder_volumes(self):
        volumes = []

        # Check to see if cinder is present
        # TODO(rchurch): Need to refactor with storage backend
        if ((StorageBackendConfig.has_backend_configured(
                self.dbapi, constants.CINDER_BACKEND_CEPH)) or
                (StorageBackendConfig.has_backend_configured(
                    self.dbapi, constants.CINDER_BACKEND_LVM))):
            search_opts = {
                'all_tenants': 1
            }
            try:
                volumes = self._get_cinderclient().volumes.list(
                    search_opts=search_opts)
            except Exception as e:
                LOG.error("get_cinder_volumes: Failed to access "
                          "Cinder client: %s" % e)

        return volumes

    def get_cinder_services(self):
        service_list = []

        # Check to see if cinder is present
        # TODO(rchurch): Need to refactor with storage backend
        if ((StorageBackendConfig.has_backend_configured(
                self.dbapi, constants.CINDER_BACKEND_CEPH)) or
                (StorageBackendConfig.has_backend_configured(
                    self.dbapi, constants.CINDER_BACKEND_LVM))):
            try:
                service_list = self._get_cinderclient().services.list()
            except Exception as e:
                LOG.error("get_cinder_services:Failed to access "
                          "Cinder client: %s" % e)

        return service_list

    def get_cinder_volume_types(self):
        """Obtain the current list of volume types."""
        volume_types_list = []

        if StorageBackendConfig.is_service_enabled(self.dbapi,
                                                   constants.SB_SVC_CINDER,
                                                   filter_shared=True):
            try:
                volume_types_list = \
                    self._get_cinderclient().volume_types.list()
            except Exception as e:
                LOG.error("get_cinder_volume_types: Failed to access "
                          "Cinder client: %s" % e)

        return volume_types_list

    #################
    # Barbican
    #################
    def _get_barbicanclient(self):
        if not self.barbican_client:
            self.barbican_client = barbican_client_v1.Client(
                session=self._get_keystone_session(),
                auth_url=self.auth_url,
                endpoint_type='internalURL',
                region_name=cfg.CONF.KEYSTONE_AUTHTOKEN.barbican_region_name)
        return self.barbican_client

    def get_barbican_secret_by_name(self, context, name):
        try:
            client = self._get_barbicanclient()
            secret_list = client.secrets.list(name=name)
            secret = next(iter(secret_list), None)
            return secret
        except Exception:
            LOG.error("Unable to find Barbican secret %s", name)
            return None

    def create_barbican_secret(self, context, name, payload):
        if not payload:
            LOG.error("Empty password is passed to Barbican %s" % name)
            return None
        try:
            client = self._get_barbicanclient()
            secret = self.get_barbican_secret_by_name(context, name)
            if secret:
                client.secrets.delete(secret.secret_ref)
            secret = client.secrets.create(name, payload)
            secret.store()
            return secret.secret_ref
        except Exception:
            LOG.error("Unable to create Barbican secret %s" % name)
            return None

    def delete_barbican_secret(self, context, name):
        try:
            client = self._get_barbicanclient()
            secret = self.get_barbican_secret_by_name(context=context,
                                                      name=name)
            if not secret:
                LOG.error("Unable to delete unknown Barbican secret %s" % name)
                return False
            client.secrets.delete(secret_ref=secret.secret_ref)
            return True
        except Exception:
            LOG.error("Unable to delete Barbican secret %s" % name)
            return False

    #########################
    # Primary Region Inventory
    # Region specific methods
    #########################
    def _get_primary_cgtsclient(self):
        # import the module in the function that uses it
        # as the cgtsclient is only installed on the controllers
        from cgtsclient.v1 import client as cgts_client
        # get region one name from platform.conf
        region1_name = get_region_name('region_1_name')
        if region1_name is None:
            region1_name = 'RegionOne'
        auth_ref = self._get_keystoneclient().auth_ref
        if auth_ref is None:
            raise exception.InventoryException(
                _("Unable to get auth ref from keystone client"))
        auth_token = auth_ref.service_catalog.get_token()
        endpoint = (auth_ref.service_catalog.
                    get_endpoints(service_type='platform',
                                  endpoint_type='internal',
                                  region_name=region1_name))
        endpoint = endpoint['platform'][0]
        version = 1
        return cgts_client.Client(version=version,
                                  endpoint=endpoint['url'],
                                  auth_url=self.auth_url,
                                  token=auth_token['id'])

    def get_ceph_mon_info(self):
        ceph_mon_info = dict()
        try:
            cgtsclient = self._get_primary_cgtsclient()
            clusters = cgtsclient.cluster.list()
            if clusters:
                ceph_mon_info['cluster_id'] = clusters[0].cluster_uuid
            else:
                LOG.error("Unable to get the cluster from the primary region")
                return None
            ceph_mon_ips = cgtsclient.ceph_mon.ip_addresses()
            if ceph_mon_ips:
                ceph_mon_info['ceph-mon-0-ip'] = ceph_mon_ips.get(
                    'ceph-mon-0-ip', '')
                ceph_mon_info['ceph-mon-1-ip'] = ceph_mon_ips.get(
                    'ceph-mon-1-ip', '')
                ceph_mon_info['ceph-mon-2-ip'] = ceph_mon_ips.get(
                    'ceph-mon-2-ip', '')
            else:
                LOG.error("Unable to get the ceph mon IPs from the primary "
                          "region")
                return None
        except Exception as e:
            LOG.error("Unable to get ceph info from the "
                      "primary region: %s" % e)
            return None
        return ceph_mon_info

    def region_has_ceph_backend(self):
        ceph_present = False
        try:
            backend_list = \
                self._get_primary_cgtsclient().storage_backend.list()
            for backend in backend_list:
                if backend.backend == constants.CINDER_BACKEND_CEPH:
                    ceph_present = True
                    break
        except Exception as e:
            LOG.error("Unable to get storage backend list from the primary "
                      "region: %s" % e)
        return ceph_present


def get_region_name(region):
    # get region name from platform.conf
    lines = [line.rstrip('\n') for line in
             open('/etc/platform/platform.conf')]
    for line in lines:
        values = line.split('=')
        if values[0] == region:
            return values[1]
    LOG.error("Unable to get %s from the platform.conf." % region)
    return None
