import ast
import socket
import subprocess
import shlex
import uuid

import bitmath
from eliot import Logger
from eliot import Message
from eliot import startTask
from flocker.node.agents import blockdevice
from twisted.python import filepath
from zope.interface import implementer

from flocker.node.agents.blockdevice import (
    AlreadyAttachedVolume, BlockDeviceVolume,
    IBlockDeviceAPI, UnknownVolume,
    UnattachedVolume, IProfiledBlockDeviceAPI)
from solidfire_flocker_driver import sfapi
from solidfire_flocker_driver import utils

ALLOCATION_UNIT = bitmath.GiB(1).bytes
logger = Logger()


def initialize_driver(cluster_id, **kwargs):
    """Initialize a new instance of the SolidFire driver.

    :param kwargs['endpoint', 'vag_name', 'account_name']
    :return: SolidFireBolockDeviceAPI object
    """
    return SolidFireBlockDeviceAPI(str(cluster_id), **kwargs)


@implementer(IBlockDeviceAPI)
@implementer(IProfiledBlockDeviceAPI)
class SolidFireBlockDeviceAPI(object):
    """ BlockDevice flocker implemenation using SolidFire."""
    def __init__(self, cluster_id, **kwargs):
        """
        :param cluster_id
        :kwargs endpoint

        Optional kwargs:
        : kwarg initiator_name
        : kwarg svip
        : kwarg profiles
        """

        self.flocker_cluster_id = cluster_id
        self.initiator_iqns = []
        self.vagname = cluster_id
        self.account_name = cluster_id
        endpoint = kwargs.get('endpoint', None)
        if not endpoint:
            raise Exception("Missing endpoint config parameters, unable "
                            "to initialize SolidFire Plugin.")
        self.endpoint_dict = self._build_endpoint_dict(endpoint)
        self.client = sfapi.SolidFireClient(self.endpoint_dict)
        self.account_id = self._init_solidfire_account(self.account_name)
        self.volume_prefix = kwargs.get('volume_prefix', '')

        if kwargs.get('initiator_name', None):
            self.initiator_iqns.append(kwargs.get('initiator_name'))
        else:
            self.initiator_iqns = self._set_initiator_iqns()

        self.svip = kwargs.get('svip', None)
        if not self.svip:
            self.svip = self._get_svip()

        self.vag_id = self._initialize_vag(self.vagname, self.initiator_iqns)
        self.profiles = self._set_profiles(kwargs.get('profiles', None))

    def _init_solidfire_account(self, account_name):
        try:

            acct_id = self.client.issue_request(
                'GetAccountByName',
                {'username': self.account_name})['account']['accountID']
        except sfapi.SolidFireRequestException:
            params = {'username': account_name,
                      'attributes': {}}
            acct_id = self.client.issue_request('AddAccount',
                                                params)['accountID']
        return acct_id

    def _build_endpoint_dict(self, endpoint_string):
        port = 443
        keys = endpoint_string.split('/')
        if len(keys) != 5:
            raise Exception

        mvip = keys[2].split('@')[1]
        if ':' in mvip:
            mvip = mvip.split(':')[0]
            port = mvip.split(':')[1]
        login = keys[2].split('@')[0].split(':')[0]
        password = keys[2].split('@')[0].split(':')[1]
        return {'mvip': mvip,
                'login': login,
                'password': password,
                'port': port,
                'url': 'https://%s:%s' % (mvip, port)}

    def _get_svip(self):
        cluster_info = self.client.issue_request('GetClusterInfo', {})
        return cluster_info['clusterInfo']['svip'] + ':3260'

    def _initialize_vag(self, vag_name, iqns):
        # Get the vag and make sure this initiators iqn(s) are present
        vag_id = None
        vag = None

        vags = self.client.issue_request(
            'ListVolumeAccessGroups',
            {},
            version='7.0')['volumeAccessGroups']
        for v in vags:
            if v['name'] == vag_name:
                vag = v
                vag_id = v['volumeAccessGroupID']

        if not vag:
            params = {'name': vag_name,
                      'initiators': iqns}
            vag_id = self.client.issue_request(
                'CreateVolumeAccessGroup',
                params,
                version='7.0')['volumeAccessGroupID']
        else:
            missing_iqns = []
            for i in iqns:
                if i not in vag['initiators']:
                    missing_iqns.append(i)
            if len(missing_iqns) >= 1:
                self.client.issue_request(
                    'AddInitiatorsToVolumeAccessGroup',
                    {'volumeAccessGroupID': vag['volumeAccessGroupID'],
                     'initiators': missing_iqns},
                    version='7.0')

        return vag_id

    def _set_initiator_iqns(self):
        iqns = []
        cmd = 'cat /etc/iscsi/initiatorname.iscsi'
        output = subprocess.check_output(shlex.split(cmd))
        entries = output.split('\n')
        for e in entries:
            if 'InitiatorName=' in e:
                iqns.append(e.split('=')[1])
        return iqns

    def _set_profiles(self, config_profiles):
        # We'll set some defaults if they didn't specify
        # anything in the config file
        profiles = {'Gold': {'minIOPS': 5000,
                             'maxIOPS': 8000,
                             'burstIOPS': 15000},
                    'Silver': {'minIOPS': 3000,
                               'maxIOPS': 5000,
                               'burstIOPS': 10000},
                    'Bronze': {'minIOPS': 1000,
                               'maxIOPS': 3000,
                               'burstIOPS': 5000}}
        if config_profiles:
            profiles = ast.literal_eval(config_profiles)
        return profiles

    def _process_profile(self, profile_name):
        profile = self.profiles.get(profile_name, None)
        if not profile:
            Message.new(Error="Requested profile not found:"
                              + str(profile_name)).write(logger)
        else:
            if not all(k in profile for k in ('minIOPS',
                                              'maxIOPS',
                                              'burstIOPS')):
                profile = None
        Message.new(Debug="Set volume profile: "
                          + str(profile)).write(logger)
        return profile

    def _get_solidfire_volume(self, solidfire_id):
        volumes = self.client.issue_request('ListVolumesForAccount',
                                            {'accountID': self.account_id},
                                            version='7.0')['volumes']
        vol = None
        for v in volumes:
            if int(v['volumeID']) == int(solidfire_id):
                vol = v
                break
        return vol

    def _current_iscsi_sessions(self, sf_volid):
        current_sessions = []
        result = self.client.issue_request('ListISCSISessions',
                                           {},
                                           version='7.0')
        for session in result['sessions']:
            if sf_volid == session['volumeID']:
                current_sessions.append(session)
        return current_sessions

    def allocation_unit(self):
        """Gets the minimum allocation unit for our backend.

        The Storage Center recommended minimum is 1 GiB.
        :returns: 1 GiB in bytes.
        """
        return ALLOCATION_UNIT

    def compute_instance_id(self):
        return unicode(socket.gethostbyname(socket.getfqdn()))

    def create_volume(self, dataset_id, size):
        """ Create a new volume on the SolidFire Cluster.
        :param UUID dataset_id: The Flocker dataset ID of the dataset on this
            volume.
        :param int size: The size of the new volume in bytes.
        :returns: A ``BlockDeviceVolume``.
        """
        return self.create_volume_with_profile(dataset_id, size, None)

    def create_volume_with_profile(self, dataset_id, size, profile_name):
        """Create a new volume with profile on the SolidFire Cluster.

        :param dataset_id: The Flocker dataset UUID for the volume.
        :param size: The size of the new volume in bytes (int).
        :param profile_name: The name of the storage profile for
                             this volume.
        :return: A ``BlockDeviceVolume``
        """

        with startTask(logger,
                       "SFAgent:create_volume_with_profile",
                       datasetID=unicode(dataset_id),
                       volSize=unicode(size),
                       profile=unicode(profile_name)):

            # NOTE(jdg): dataset_id is unique so we use it as the
            # volume name on the cluster.  We then use the resultant
            # solidfire vol ID as the blockdevice_id
            vname = '%s%s' % (self.volume_prefix, dataset_id)
            profile = self._process_profile(profile_name)
            params = {'name': vname,
                      'accountID': self.account_id,
                      'sliceCount': 1,
                      'totalSize': int(size),
                      'enable512e': True,
                      'attributes': {}}
            if profile:
                # We set these keys explicity from the profile, rather
                # than slurping in a dict inentionally, this handles
                # cases where there may be extra/invalid keys in the
                # profile.  More importantly alows us to extend the
                # usage of profiles beyond QoS later.
                params['qos'] = {'minIOPS': profile['minIOPS'],
                                 'maxIOPS': profile['maxIOPS'],
                                 'burstIOPS': profile['burstIOPS']}
            result = self.client.issue_request('CreateVolume',
                                               params)

            params = {}
            params['volumeAccessGroupID'] = self.vag_id
            params['volumes'] = [int(result['volumeID'])]
            self.client.issue_request('AddVolumesToVolumeAccessGroup',
                                      params,
                                      version='7.0')
            return BlockDeviceVolume(
                blockdevice_id=unicode(result['volumeID']),
                size=size,
                attached_to=None,
                dataset_id=uuid.UUID(dataset_id))

    def destroy_volume(self, blockdevice_id):
        """ Destroy an existing volume.

        :param unicode blockdevice_id: The unique identifier for the volume to
            destroy.

        :return: ``None``
        """
        params = {'volumeID': int(blockdevice_id)}
        try:
            self.client.issue_request('DeleteVolume', params)
        except sfapi.SolidFireRequestException as ex:
            if 'xVolumeIDDoesNotExist' in ex.msg:
                raise UnknownVolume(blockdevice_id)
            else:
                raise ex
        return

    def get_device_path(self, blockdevice_id):
        vol = self._get_solidfire_volume(blockdevice_id)
        if not vol:
            raise UnknownVolume(blockdevice_id)
        disk_by_path = utils.get_expected_disk_path(self.svip, vol['iqn'])
        return filepath.FilePath(
            utils.get_device_file_from_path(disk_by_path)).realpath()

    def attach_volume(self, blockdevice_id, attach_to):
        """ Attach ``blockdevice_id`` to ``host``.

        :param unicode blockdevice_id: The unique identifier for the block
            device being attached.
        :param unicode attach_to: An identifier like the one returned by the
            ``compute_instance_id`` method indicating the node to which to
            attach the volume.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises AlreadyAttachedVolume: If the supplied ``blockdevice_id`` is
            already attached.
        :returns: A ``BlockDeviceVolume`` with a ``host`` attribute set to
            ``host``.
        """

        vol = self._get_solidfire_volume(blockdevice_id)
        if not vol:
            raise UnknownVolume(blockdevice_id)

        tgt_iqn = vol['iqn']
        if utils.path_exists('/dev/disk/by-path/ip-%s-iscsi-%s-lun-0' %
                             (self.svip, tgt_iqn), 1):
            raise AlreadyAttachedVolume(blockdevice_id)

        # It's not attached here, make sure it's not attached somewhere else
        # In the future we can add multi-attach support maybe, but for now
        # avoid the trouble of file-systems etc
        current_sessions = self._current_iscsi_sessions(blockdevice_id)
        if current_sessions:
            raise AlreadyAttachedVolume(blockdevice_id)

        targets = utils.iscsi_discovery(self.svip)
        if len(targets) < 1 and tgt_iqn not in targets:
            raise Exception("No targes found during discovery.")
        if utils.iscsi_login(self.svip, tgt_iqn):
            return BlockDeviceVolume(
                blockdevice_id=unicode(blockdevice_id),
                size=vol['totalSize'],
                attached_to=attach_to,
                dataset_id=uuid.UUID(str(vol['name'])))
        raise Exception("Failed iSCSI login to device: %s" % blockdevice_id)

    def detach_volume(self, blockdevice_id):
        """ Detach ``blockdevice_id`` from whatever host it is attached to.

        :param unicode blockdevice_id: The unique identifier for the block
            device being detached.

        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises UnattachedVolume: If the supplied ``blockdevice_id`` is
            not attached to anything.
        :returns: ``None``
        """
        vol = self._get_solidfire_volume(blockdevice_id)
        if not vol:
            raise UnknownVolume(blockdevice_id)

        tgt_iqn = vol['iqn']
        if not utils.path_exists('/dev/disk/by-path/ip-%s-iscsi-%s-lun-0' %
                                 (self.svip, tgt_iqn), 1):
            raise UnattachedVolume(blockdevice_id)
        utils.iscsi_logout(self.svip, tgt_iqn)

    def resize_volume(self, blockdevice_id, size):
        """ Resize ``blockdevice_id``.

        This changes the amount of storage available.  It does not change the
        data on the volume (including the filesystem).

        :param unicode blockdevice_id: The unique identifier for the block
            device being detached.
        :param int size: The required size, in bytes, of the volume.

        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.

        :returns: ``None``
        """
        vol = self._get_solidfire_volume(blockdevice_id)
        if not vol:
            raise UnknownVolume(blockdevice_id)

        if size <= vol['totalSize']:
            raise blockdevice.VolumeException(blockdevice_id)

        params = {'volumeID': vol['volumeID'],
                  'totalSize': int(size)}
        self.client.issue_request('ModifyVolume',
                                  params, version='5.0')

    def list_volumes(self):
        """ List all the block devices available via the back end API.

        :returns: A ``list`` of ``BlockDeviceVolume``s.
        """
        volumes = []
        sfvols = self.client.issue_request('ListVolumesForAccount',
                                           {'accountID': self.account_id},
                                           version='7.0')['volumes']
        for v in sfvols:
            attached_to = None
            tgt_iqn = v['iqn']
            if utils.path_exists('/dev/disk/by-path/ip-%s-iscsi-%s-lun-0' %
                                 (self.svip, tgt_iqn), 1):
                attached_to = self.compute_instance_id()
            name = v['name']
            if 'flock-' in v['name']:
                name = name.replace('flock-', '')
            volumes.append(BlockDeviceVolume(
                           blockdevice_id=unicode(v['volumeID']),
                           size=v['totalSize'],
                           attached_to=attached_to,
                           dataset_id=uuid.UUID(str(name))))
        return volumes
