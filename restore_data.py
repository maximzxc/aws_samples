import time
import logging
import boto.ec2
import boto.exception

from .config import (
    INSTANCE_TYPE, DATA_VOLUME_SIZE, DATA_VOLUME_RATE, DATA_VOLUME_TYPE,
    AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_AMI_IMAGE_ID,
    AWS_KEY_NAME, AWS_SECURITY_GROUPS, AWS_SBNET_ID
)

logger = logging.getLogger('maas')


USER_SCRIPT_TEMPLATE_RECOVERY_CASE = """#!/bin/bash -ex
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

## setup the ebs volume for data.
avail_blk=`lsblk -n -oNAME,MOUNTPOINT | grep -v '/$' | grep -v 'xvda' | awk -F' ' '{{print $1}}'`
if [ -z "$avail_blk" ]; then
    echo "Don't have a mounted data blk device."
    exit -1
fi

cp /etc/fstab /etc/fstab.orig
echo "/dev/$avail_blk /mnt/data ext4 defaults,nofail,nobootwait 0 2" >> /etc/fstab
mount -a
echo "{some_variable_to_pass}" >> /dev/null
sudo service supervisor start
supervisorctl start all
"""

def create_bdm(size, type, rate, non_root_snap_id):
    """ Create a pair of block-devices to attach to the instance.
    """
    retry = 0
    while (retry < 3):
        try:
            bd_root = boto.ec2.blockdevicemapping.BlockDeviceType()
            bd_nonroot = boto.ec2.blockdevicemapping.BlockDeviceType()
            size = int(size[:-1])
            bd_nonroot.size = size
            bd_nonroot.volume_type = type
            if type == 'io1':
                bd_nonroot.iops = rate
            bd_nonroot.delete_on_termination = False
            if non_root_snap_id:
                bd_nonroot.snapshot_id = non_root_snap_id
            bdmapping = boto.ec2.blockdevicemapping.BlockDeviceMapping()
            bdmapping['/dev/sda1'] = bd_root
            bdmapping['/dev/xvdf'] = bd_nonroot
            return bdmapping
        except (boto.exception.EC2ResponseError, AssertionError) as e:
            retry += 1
            logger.exception(e)
            logger.error(e)


def extract_non_root_id(bdm):
    """
    Get id of non root volume (data volume in our case)
    """
    try:
        bd = bdm['blockDeviceMapping']['/dev/xvdf']
        return bd.volume_id
    # case when we don't have data volume is also possible
    except KeyError:
        return None


def try_to_stop_ec2_instance(user, conn, non_root_snap_id, instance_id):
    try:
        conn.stop_instances([instance_id])
        reservations = conn.get_all_reservations([instance_id])
        instance = reservations[0].instances[0]
        while instance.update() != "stopped":
            time.sleep(5)
        return "stopped"
    except (boto.exception.EC2ResponseError, AssertionError) as e:
        logger.exception(e)
        logger.error(e)


def try_to_create_ec2_instance(non_root_snap_id, instance_id):

    try:
        conn = boto.ec2.connect_to_region(
            AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
        )

        try_to_stop_ec2_instance(conn, non_root_snap_id,
                                 instance_id)

        instance_type = INSTANCE_TYPE
        data_volume_size = DATA_VOLUME_SIZE
        data_volume_rate = DATA_VOLUME_RATE
        data_volume_type = DATA_VOLUME_TYPE

        bdm = create_bdm(data_volume_size, data_volume_type,
                         data_volume_rate, non_root_snap_id)

        some_variable_to_pass = "here is some data that you want to pass to server"

        cmd = USER_SCRIPT_TEMPLATE_RECOVERY_CASE.format(
            some_variable_to_pass=some_variable_to_pass
        )

        logger.debug(cmd)
        user_data = cmd

        reservation = conn.run_instances(
            AWS_AMI_IMAGE_ID,
            instance_type=instance_type,
            key_name=AWS_KEY_NAME,
            security_group_ids=AWS_SECURITY_GROUPS,
            subnet_id=AWS_SBNET_ID,
            block_device_map=bdm,
            user_data=user_data,
        )

        instance = reservation.instances[0]

        while instance.update() != "running":
            time.sleep(5)


        # Check that instances got an IP and proper name
        assert instance.ip_address is not None
        assert instance.update() == "running"

        return instance

    except (boto.exception.EC2ResponseError, AssertionError) as e:
        logger.exception(e)
        logger.error(e)
        return None


def create_ec2_instance(non_root_snap_id, instance_id, max_retry=5):

    retry = 0

    while(retry < max_retry):
        instance = try_to_create_ec2_instance(non_root_snap_id,
                                              instance_id)
        if instance:
            return instance

    raise Exception("Can't create an instance")
