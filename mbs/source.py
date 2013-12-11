__author__ = 'abdul'

from base import MBSObject
from errors import BlockStorageSnapshotError

from target import EbsSnapshotReference, LVMSnapshotReference
from mbs import get_mbs
from errors import *

import mongo_uri_tools
import mbs_logging

from boto.ec2 import connect_to_region
from utils import (
    freeze_mount_point, unfreeze_mount_point, export_mbs_object_list,
    suspend_lvm_mount_point, resume_lvm_mount_point
)
###############################################################################
# LOGGER
###############################################################################
logger = mbs_logging.logger

###############################################################################
# Backup Source Classes
###############################################################################
class BackupSource(MBSObject):

    ###########################################################################
    def __init__(self):
        MBSObject.__init__(self)
        self._cloud_block_storage = None

    ###########################################################################
    @property
    def uri(self):
        return None

    ###########################################################################
    @property
    def database_name(self):
        return None

    ###########################################################################
    @property
    def collection_name(self):
        return None

    ###########################################################################
    @property
    def cloud_block_storage(self):
        """
            OPTIONAL: Represents cloud block storage for the source
        """
        return self._cloud_block_storage

    @cloud_block_storage.setter
    def cloud_block_storage(self, val):
        self._cloud_block_storage = val

    ###########################################################################
    def get_block_storage_by_address(self, address):
        block_storage = self.cloud_block_storage
        if block_storage is None:
            return None
        elif isinstance(block_storage, dict):
            return block_storage.get(address)
        elif isinstance(block_storage, CloudBlockStorage):
            return block_storage
        else:
            msg = ("Invalid cloudBlockStorageConfig. Must be a "
                   "CloudBlockStorage or a dict of address=>CloudBlockStorage")
            raise ConfigurationError(msg)

    ###########################################################################
    def to_document(self, display_only=False):
        doc = {}

        if self.cloud_block_storage:
            doc["cloudBlockStorage"] = self._export_cloud_block_storage()

        return doc

    ###########################################################################
    def _export_cloud_block_storage(self, display_only=False):
        cbs = self.cloud_block_storage
        if isinstance(cbs, CloudBlockStorage):
            return cbs.to_document(display_only=display_only)
        elif isinstance(cbs, dict):
            return dict((key, value.to_document(display_only=display_only))
                            for (key, value) in cbs.items())
        else:
            msg = ("Invalid cloudBlockStorageConfig. Must be a "
                   "CloudBlockStorage or a dict of address=>CloudBlockStorage")
            raise ConfigurationError(msg)

    ###########################################################################
    def is_valid(self):
        errors = self.validate()
        if errors:
            return False
        else:
            return True

    ###########################################################################
    def validate(self):
        """
         Returns an array containing validation messages (if any). Empty if no
         validation errors
        """
        return []



###############################################################################
# MongoSource
###############################################################################
class MongoSource(BackupSource):

    ###########################################################################
    def __init__(self, uri=None):
        BackupSource.__init__(self)
        self._uri = uri

    ###########################################################################
    @property
    def uri(self):
        return self._uri

    @uri.setter
    def uri(self, uri):
        self._uri = uri

    ###########################################################################
    def to_document(self, display_only=False):
        doc =  super(MongoSource, self).to_document()
        doc.update ({
            "_type": "MongoSource",
            "uri": (mongo_uri_tools.mask_mongo_uri(self.uri) if display_only
                    else self.uri)
        })

        return doc

    ###########################################################################
    def validate(self):
        errors = []
        if not self.uri:
            errors.append("Missing 'uri' property")
        elif not mongo_uri_tools.is_mongo_uri(self.uri):
            errors.append("Invalid uri '%s'" % self.uri)

        return errors

###############################################################################
# CloudBlockStorageSource
###############################################################################
class CloudBlockStorage(MBSObject):
    """
        Base class for Cloud Block Storage
    """
    ###########################################################################
    def __init__(self):
        MBSObject.__init__(self)
        self._mount_point = None

    ###########################################################################
    def create_snapshot(self, name, description):
        """
            Create a snapshot for the volume with the specified description.
            Returns a CloudBlockStorageSnapshotReference
             Must be implemented by subclasses
        """

    ###########################################################################
    def delete_snapshot(self, snapshot_ref):
        """
            deletes the snapshot reference
            Must be implemented by subclasses
        """

    ###########################################################################
    def check_snapshot_updates(self, snapshot_ref):
        """
            Checks status updates to snapshot and populates reference with new
            updates.
            Returns true if there were new updates
        """

    ###########################################################################
    def suspend_io(self):
        """
           suspends the underlying IO
        """

    ###########################################################################
    def resume_io(self):
        """
            resumes the underlying IO
        """

    ###########################################################################
    @property
    def mount_point(self):
        return self._mount_point

    @mount_point.setter
    def mount_point(self, val):
        self._mount_point = val

    ###########################################################################
    def to_document(self, display_only=False):
        return {
            "mountPoint": self.mount_point
        }

###############################################################################
# EbsVolumeStorage
###############################################################################
class EbsVolumeStorage(CloudBlockStorage):

    ###########################################################################
    def __init__(self):
        CloudBlockStorage.__init__(self)
        self._encrypted_access_key = None
        self._encrypted_secret_key = None
        self._volume_id = None
        self._region = None
        self._ec2_connection = None

    ###########################################################################
    @property
    def volume_id(self):
        return self._volume_id

    @volume_id.setter
    def volume_id(self, volume_id):
        self._volume_id = str(volume_id)

    ###########################################################################
    def create_snapshot(self, name, description):
        ebs_volume = self._get_ebs_volume()

        logger.info("Creating EBS snapshot for volume '%s'" % self.volume_id)

        ebs_snapshot = ebs_volume.create_snapshot(description)
        if not ebs_snapshot:
            raise BlockStorageSnapshotError("Failed to create snapshot from "
                                            "backup source :\n%s" % self)

        # add name tag
        ebs_snapshot.add_tag("Name", name)

        logger.info("Snapshot kicked off successfully for volume '%s'. "
                    "Snapshot id '%s'." % (self.volume_id, ebs_snapshot.id))

        ebs_ref = self._new_ebs_snapshot_reference(ebs_snapshot)

        return ebs_ref

    ###########################################################################
    def delete_snapshot(self, snapshot_ref):
        snapshot_id = snapshot_ref.snapshot_id
        try:
            logger.info("Deleting snapshot '%s' " % snapshot_id)
            self.ec2_connection.delete_snapshot(snapshot_id)
            logger.info("Snapshot '%s' deleted successfully!" % snapshot_id)
            return True
        except Exception, e:
            if "does not exist" in str(e):
                logger.warning("Snapshot '%s' does not exist" % snapshot_id)
                return False
            else:
                msg = "Error while deleting snapshot '%s'" % snapshot_id
                raise BlockStorageSnapshotError(msg, cause=e)

    ###########################################################################
    def check_snapshot_updates(self, ebs_ref):
        """
            Detects changes in snapshot
        """
        ebs_snapshot = self.get_ebs_snapshot_by_id(ebs_ref.snapshot_id)
        # NOTE check if the above call returns a snapshot object because boto
        # returns None although the snapshot exists (AWS api freakiness ?)
        if ebs_snapshot:
            new_ebs_ref = self._new_ebs_snapshot_reference(ebs_snapshot)
            if new_ebs_ref != ebs_ref:
                return new_ebs_ref

    ###########################################################################
    def _new_ebs_snapshot_reference(self, ebs_snapshot):
        return EbsSnapshotReference(snapshot_id=ebs_snapshot.id,
                                    cloud_block_storage=self,
                                    status=ebs_snapshot.status,
                                    start_time=ebs_snapshot.start_time,
                                    volume_size=ebs_snapshot.volume_size,
                                    progress=ebs_snapshot.progress)

    ###########################################################################
    @property
    def volume_id(self):
        return self._volume_id

    @volume_id.setter
    def volume_id(self, volume_id):
        self._volume_id = str(volume_id)

    ###########################################################################
    @property
    def region(self):
        return self._region

    @region.setter
    def region(self, region):
        self._region = str(region)

    ###########################################################################
    @property
    def access_key(self):
        if self.encrypted_access_key:
            return get_mbs().encryptor.decrypt_string(self.encrypted_access_key)

    @access_key.setter
    def access_key(self, access_key):
        if access_key:
            eak = get_mbs().encryptor.encrypt_string(str(access_key))
            self.encrypted_access_key = eak

    ###########################################################################
    @property
    def secret_key(self):
        if self.encrypted_secret_key:
            return get_mbs().encryptor.decrypt_string(self.encrypted_secret_key)

    @secret_key.setter
    def secret_key(self, secret_key):
        if secret_key:
            sak = get_mbs().encryptor.encrypt_string(str(secret_key))
            self.encrypted_secret_key = sak

    ###########################################################################
    @property
    def encrypted_access_key(self):
        return self._encrypted_access_key

    @encrypted_access_key.setter
    def encrypted_access_key(self, val):
        if val:
            self._encrypted_access_key = val.encode('ascii', 'ignore')

    ###########################################################################
    @property
    def encrypted_secret_key(self):
        return self._encrypted_secret_key

    @encrypted_secret_key.setter
    def encrypted_secret_key(self, val):
        if val:
            self._encrypted_secret_key = val.encode('ascii', 'ignore')

    ###########################################################################
    @property
    def ec2_connection(self):
        if not self._ec2_connection:
            conn = connect_to_region(self.region,
                                     aws_access_key_id=self.access_key,
                                     aws_secret_access_key=self.secret_key)
            if not conn:
                raise ConfigurationError("Invalid region in block storage %s" %
                                         self)
            self._ec2_connection = conn

        return self._ec2_connection


    ###########################################################################
    def _get_ebs_volume(self):
        volumes = self.ec2_connection.get_all_volumes([self.volume_id])

        if volumes is None or len(volumes) == 0:
            raise Exception("Could not find volume %s" % self.volume_id)

        return volumes[0]

    ###########################################################################
    def get_ebs_snapshots(self):
        filters = {
            "volume-id": self.volume_id
        }
        return self.ec2_connection.get_all_snapshots(filters=filters)

    ###########################################################################
    def get_ebs_snapshot_by_id(self, snapshot_id):
        filters = {
            "volume-id": self.volume_id,
            "snapshot-id": snapshot_id
        }
        snapshots= self.ec2_connection.get_all_snapshots(filters=filters)

        if snapshots:
            return snapshots[0]

    ###########################################################################
    def suspend_io(self):
        logger.info("Suspend IO for volume '%s' using fsfreeze" %
                    self.volume_id)
        freeze_mount_point(self.mount_point)

    ###########################################################################
    def resume_io(self):

        logger.info("Resume io for volume '%s' using fsfreeze" %
                    self.volume_id)

        unfreeze_mount_point(self.mount_point)

    ###########################################################################
    def to_document(self, display_only=False):
        doc = super(EbsVolumeStorage, self).to_document(display_only=
                                                        display_only)

        ak = "xxxxx" if display_only else self.encrypted_access_key
        sk = "xxxxx" if display_only else self.encrypted_secret_key
        doc.update({
            "_type": "EbsVolumeStorage",
            "volumeId": self.volume_id,
            "region": self.region,
            "encryptedAccessKey": ak,
            "encryptedSecretKey": sk
        })

        return doc


###############################################################################
# LVMStorage
###############################################################################
class LVMStorage(CloudBlockStorage):
    ###########################################################################
    def __init__(self):
        CloudBlockStorage.__init__(self)
        self._constituents = None

    ###########################################################################
    def create_snapshot(self, name, description):
        """
            Creates a LVMSnapshotReference composed of all
            constituent snapshots
        """
        logger.info("Creating LVM Snapshot name='%s', description='%s' "
                    "for LVMStorage: \n%s" % (name, description, str(self)))
        logger.info("Creating snapshots for all constituents...")
        constituent_snapshots = []
        for constituent in self.constituents:
            logger.info("Creating snapshot constituent: \n%s" %
                        str(constituent))
            snapshot = constituent.create_snapshot(name, description)
            constituent_snapshots.append(snapshot)


        lvm_snapshot = LVMSnapshotReference(self,
                                            constituent_snapshots=
                                            constituent_snapshots)

        logger.info("Successfully created LVM Snapshot \n%s" %
                    str(lvm_snapshot))

        return lvm_snapshot

    ###########################################################################
    def delete_snapshot(self, snapshot_ref):
        for constituent_snapshot in snapshot_ref.constituent_snapshots:
            constituent = constituent_snapshot.cloud_block_storage
            constituent.delete_snapshot(constituent_snapshot)


    ###########################################################################
    def check_snapshot_updates(self, snapshot_ref):
        new_constituent_snapshots = []
        has_changes = False
        for constituent_snapshot in snapshot_ref.constituent_snapshots:
            constituent = constituent_snapshot.cloud_block_storage
            new_constituent_snapshot = \
                constituent.check_snapshot_updates(constituent)
            if new_constituent_snapshot:
                has_changes = True
            else:
                new_constituent_snapshot = constituent_snapshot

            new_constituent_snapshots.append(new_constituent_snapshot)

        if has_changes:
            return LVMSnapshotReference(self,
                                        constituent_snapshots=
                                        new_constituent_snapshots)

    ###########################################################################
    def suspend_io(self):
        logger.info("Suspend IO for LVM '%s' using dmsetup" %
                    self.mount_point)
        suspend_lvm_mount_point(self.mount_point)

    ###########################################################################
    def resume_io(self):

        logger.info("Resume io for LVM '%s' using dmsetup" %
                    self.mount_point)

        resume_lvm_mount_point(self.mount_point)

    ###########################################################################
    @property
    def constituents(self):
        return self._constituents


    @constituents.setter
    def constituents(self, val):
        self._constituents = val

    ###########################################################################
    def _export_constituents(self, display_only=False):
        return export_mbs_object_list(self.constituents,
                                      display_only=display_only)

    ###########################################################################
    def to_document(self, display_only=False):
        doc = super(LVMStorage, self).to_document(
            display_only=display_only)

        doc.update({
            "_type": "LVMStorage",
            "constituents": self._export_constituents(
                display_only=display_only)
        })

        return doc
