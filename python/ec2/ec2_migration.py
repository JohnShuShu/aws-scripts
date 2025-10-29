#!/usr/bin/env python3
"""
EC2 Instance Migration Script
Migrates instances with names prefixed with 'branch' to new instance types and gp3 volumes
"""

import boto3
import time
from botocore.exceptions import ClientError

# Instance type mapping
INSTANCE_TYPE_MAP = {
    'm4.2xlarge': 'r6i.xlarge',
    'm4.xlarge': 'r6i.large',
    'm5.4xlarge': 'r5.2xlarge'
}

# Configuration
WAIT_TIME_AFTER_STOP = 30  # seconds to wait after stopping
WAIT_TIME_AFTER_MODIFICATIONS = 60  # seconds to wait after all modifications
PREFIX = 'branch'

def get_instance_name(instance):
    """Extract the Name tag from an instance"""
    if 'Tags' in instance:
        for tag in instance['Tags']:
            if tag['Key'] == 'Name':
                return tag['Value']
    return None

def stop_instance(ec2_client, instance_id, instance_name):
    """Stop an EC2 instance"""
    print(f"Stopping instance {instance_name} ({instance_id})...")
    try:
        ec2_client.stop_instances(InstanceIds=[instance_id])
        
        # Wait for instance to be stopped
        waiter = ec2_client.get_waiter('instance_stopped')
        waiter.wait(InstanceIds=[instance_id])
        print(f"Instance {instance_name} stopped successfully")
        return True
    except ClientError as e:
        print(f"Error stopping instance {instance_name}: {e}")
        return False

def change_instance_type(ec2_client, instance_id, instance_name, current_type, new_type):
    """Change the instance type"""
    print(f"Changing instance type for {instance_name} from {current_type} to {new_type}...")
    try:
        ec2_client.modify_instance_attribute(
            InstanceId=instance_id,
            InstanceType={'Value': new_type}
        )
        print(f"Instance type changed successfully for {instance_name}")
        return True
    except ClientError as e:
        print(f"Error changing instance type for {instance_name}: {e}")
        return False

def convert_volumes_to_gp3(ec2_client, instance_id, instance_name):
    """Convert all gp2 volumes attached to instance to gp3"""
    print(f"Converting volumes to gp3 for {instance_name}...")
    try:
        # Get all volumes attached to the instance
        response = ec2_client.describe_volumes(
            Filters=[
                {'Name': 'attachment.instance-id', 'Values': [instance_id]}
            ]
        )
        
        volumes_converted = 0
        for volume in response['Volumes']:
            volume_id = volume['VolumeId']
            volume_type = volume['VolumeType']
            
            if volume_type == 'gp2':
                print(f"  Converting volume {volume_id} from gp2 to gp3...")
                ec2_client.modify_volume(
                    VolumeId=volume_id,
                    VolumeType='gp3'
                )
                volumes_converted += 1
            else:
                print(f"  Volume {volume_id} is already {volume_type}, skipping...")
        
        if volumes_converted > 0:
            print(f"Initiated conversion of {volumes_converted} volume(s) for {instance_name}")
        else:
            print(f"No gp2 volumes found for {instance_name}")
        
        return True
    except ClientError as e:
        print(f"Error converting volumes for {instance_name}: {e}")
        return False

def start_instance(ec2_client, instance_id, instance_name):
    """Start an EC2 instance"""
    print(f"Starting instance {instance_name} ({instance_id})...")
    try:
        ec2_client.start_instances(InstanceIds=[instance_id])
        
        # Wait for instance to be running
        waiter = ec2_client.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        print(f"Instance {instance_name} started successfully")
        return True
    except ClientError as e:
        print(f"Error starting instance {instance_name}: {e}")
        return False

def process_instance(ec2_client, instance):
    """Process a single instance"""
    instance_id = instance['InstanceId']
    instance_name = get_instance_name(instance)
    current_type = instance['InstanceType']
    
    # Check if name starts with prefix
    if not instance_name or not instance_name.startswith(PREFIX) or instance_name in ["branch2.testtss.com", "branch1.devtss.com", "branch4.devtss.com"]:
        print(f"Skipping instance {instance_id} - name '{instance_name}' doesn't start with '{PREFIX}'")
        return False
    
    print(f"\n{'='*60}")
    print(f"Processing instance: {instance_name} ({instance_id})")
    print(f"Current instance type: {current_type}")
    print(f"{'='*60}")
    
    # Check if instance type needs to be changed
    if current_type not in INSTANCE_TYPE_MAP:
        print(f"Warning: Instance type {current_type} not in mapping, will only convert volumes")
        new_type = None
    else:
        new_type = INSTANCE_TYPE_MAP[current_type]
    
    # Step 1: Stop the instance
    if not stop_instance(ec2_client, instance_id, instance_name):
        return False
    
    # Wait a bit after stopping
    print(f"Waiting {WAIT_TIME_AFTER_STOP} seconds after stop...")
    time.sleep(WAIT_TIME_AFTER_STOP)
    
    # Step 2: Change instance type if needed
    if new_type:
        if not change_instance_type(ec2_client, instance_id, instance_name, current_type, new_type):
            print(f"Failed to change instance type, but will continue with volume conversion...")
    
    # Step 3: Convert volumes to gp3
    if not convert_volumes_to_gp3(ec2_client, instance_id, instance_name):
        print(f"Failed to convert volumes for {instance_name}")
    
    # Step 4: Wait for processing to complete
    print(f"Waiting {WAIT_TIME_AFTER_MODIFICATIONS} seconds for modifications to complete...")
    time.sleep(WAIT_TIME_AFTER_MODIFICATIONS)
    
    # Step 5: Start the instance
    if not start_instance(ec2_client, instance_id, instance_name):
        return False
    
    print(f"Successfully processed instance {instance_name}")
    return True

def main():
    """Main function"""
    print("EC2 Instance Migration Script")
    print(f"Looking for instances with names starting with '{PREFIX}'...\n")
    
    # Initialize boto3 EC2 client
    ec2_client = boto3.client('ec2')
    
    try:
        # Get all instances with names starting with prefix
        response = ec2_client.describe_instances(
            Filters=[
                {'Name': 'tag:Name', 'Values': [f'{PREFIX}*']}
            ]
        )
        
        instances_to_process = []
        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                # Skip terminated instances
                if instance['State']['Name'] not in ['terminated', 'terminating']:
                    instances_to_process.append(instance)
        
        if not instances_to_process:
            print(f"No instances found with names starting with '{PREFIX}'")
            return
        
        print(f"Found {len(instances_to_process)} instance(s) to process\n")
        
        # Process each instance
        success_count = 0
        for instance in instances_to_process:
            if process_instance(ec2_client, instance):
                success_count += 1
        
        print(f"\n{'='*60}")
        print(f"Migration complete!")
        print(f"Successfully processed: {success_count}/{len(instances_to_process)} instances")
        print(f"{'='*60}")
        
    except ClientError as e:
        print(f"Error accessing AWS: {e}")
        return

if __name__ == "__main__":
    main()
